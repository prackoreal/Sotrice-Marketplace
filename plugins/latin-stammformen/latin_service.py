"""The command layer of the Stammformen trainer: everything the UI can ask
for, as plain Python with no Core connection (latin_stammformen.py wires
this to Core attributes). Keeping it Core-free is what lets
test_latin_stammformen.py drive the whole feature set -- imports, edits,
quiz checks, the photo-scan pipeline with a fake AI -- directly.

Division of labour: the UI is a view. This layer is the single authority on
what a valid verb is, which ids exist, what counts as a correct answer, and
what is persisted (latin_store.py). The UI never computes any of that.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import latin_logic as L
from latin_store import ScanCache, Store, StoreError, atomic_write_text

# ai-test gained generic `attachments` in 0.1.2; an older one would silently
# drop the image and Gemini would then invent verbs for a page it never saw.
MIN_AI_VERSION = "0.1.2"
MAX_PREVIEWS = 6
SCAN_TIMEOUT_S = 120.0


class CommandError(Exception):
    """A request that can't be done, with a message fit to show the user."""


@dataclass
class AiReply:
    text: Optional[str]
    error: Optional[str] = None
    model: Optional[str] = None
    usage: Optional[dict] = None  # {"prompt": n, "output": n, "total": n} when ai-test reports it


class AiBridge(Protocol):
    def status(self) -> dict:
        """{state: ready|missing|old|no_key|starting, message, version,
        key_configured, cost_bias}"""

    def ask(self, prompt: str, attachments: list[dict], task_hint: str, timeout_s: float) -> AiReply: ...


class NoAi:
    """Used when the Core connection to ai-test isn't available (tests)."""

    def status(self) -> dict:
        return {"state": "missing", "message": "The AI spin isn't available.", "version": None, "key_configured": False, "cost_bias": "auto"}

    def ask(self, prompt: str, attachments: list[dict], task_hint: str, timeout_s: float) -> AiReply:
        raise CommandError("The AI spin isn't available.")


def downloads_dir() -> Path:
    override = os.environ.get("SOTRICE_LATIN_EXPORT_DIR")
    if override:
        return Path(override)
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    return home / "Downloads"


@dataclass
class Preview:
    kind: str
    title: str
    rows: list[dict]
    orig: list[dict]
    extras: dict = field(default_factory=dict)
    scan: Optional[dict] = None


class Service:
    def __init__(
        self,
        store: Store,
        cache: ScanCache,
        ai: Optional[AiBridge] = None,
        app_version: str = "",
        clock: Callable[[], float] = time.time,
        export_dir: Optional[Path] = None,
    ) -> None:
        self.store = store
        self.cache = cache
        self.ai: AiBridge = ai or NoAi()
        self.app_version = app_version
        self.clock = clock
        self.export_dir = export_dir
        self.previews: "OrderedDict[str, Preview]" = OrderedDict()
        self.lock = threading.Lock()
        self.scan_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def handle(self, cmd: dict, progress: Optional[Callable[[dict], None]] = None) -> dict:
        """Runs one command and returns its reply payload. Raises
        CommandError / StoreError with a user-readable message."""
        name = cmd.get("cmd")
        handler = getattr(self, f"cmd_{name}", None) if isinstance(name, str) and re.fullmatch(r"[a-z_]+", name) else None
        if handler is None:
            raise CommandError(f"Unknown command '{str(name)[:40]}'.")
        return handler(cmd, progress or (lambda _p: None)) or {}

    # ------------------------------------------------------------------
    # Simple edits
    # ------------------------------------------------------------------
    def cmd_state(self, cmd: dict, progress) -> dict:
        return {}

    def cmd_set_mastered(self, cmd: dict, progress) -> dict:
        self.store.set_mastered(self._str(cmd, "id"), bool(cmd.get("value")))
        return {}

    def cmd_set_excluded(self, cmd: dict, progress) -> dict:
        lessons: dict[int, bool] = {}
        for key, flag in (cmd.get("lessons") or {}).items() if isinstance(cmd.get("lessons"), dict) else []:
            n = L.to_lesson(key)
            if n is not None:
                lessons[n] = bool(flag)
        groups = {k: bool(v) for k, v in (cmd.get("groups") or {}).items() if isinstance(k, str)} if isinstance(cmd.get("groups"), dict) else {}
        self.store.set_excluded(lessons, groups)
        return {}

    def cmd_create_group(self, cmd: dict, progress) -> dict:
        gid = self.store.create_lesson_group(L.clean_text(cmd.get("name"), L.MAX_GROUP_NAME), cmd.get("min_lec"), cmd.get("max_lec"))
        return {"group_id": gid}

    def cmd_delete_group(self, cmd: dict, progress) -> dict:
        deleted = self.store.delete_group(self._str(cmd, "id"), bool(cmd.get("delete_verbs")))
        return {"deleted_verbs": deleted}

    def cmd_set_title(self, cmd: dict, progress) -> dict:
        self.store.set_title(L.clean_text(cmd.get("title"), L.MAX_TITLE))
        return {}

    def cmd_add_verb(self, cmd: dict, progress) -> dict:
        clean, issues = L.normalize_verb(cmd.get("fields"), {"cat": L.DEFAULT_CAT_PASTE, "lec": 1})
        self._raise_first_error(issues)
        ids = self.store.add_verbs([clean], "manual", "")
        return {"id": ids[0]}

    def cmd_update_verb(self, cmd: dict, progress) -> dict:
        fields = cmd.get("fields")
        if not isinstance(fields, dict):
            raise CommandError("Nothing to change.")
        self.store.update_verb(self._str(cmd, "id"), fields)
        return {}

    def cmd_delete_verb(self, cmd: dict, progress) -> dict:
        self.store.delete_verb(self._str(cmd, "id"))
        return {}

    # ------------------------------------------------------------------
    # Test mode
    # ------------------------------------------------------------------
    def cmd_check(self, cmd: dict, progress) -> dict:
        verb_id = self._str(cmd, "verb_id")
        verb = self.store.verb(verb_id)
        if verb is None:
            raise CommandError("That verb no longer exists.")
        answers = cmd.get("answers") if isinstance(cmd.get("answers"), dict) else {}
        verdict = L.check_answers(verb, answers)
        self.store.append_result(verb_id, verdict["ok"], answers, verdict["empty"])
        return {"verdict": verdict}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------
    def cmd_export(self, cmd: dict, progress) -> dict:
        payload = self.store.export_payload(include_results=cmd.get("include_results", True) is not False, app_version=self.app_version)
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
        directory = self.export_dir or downloads_dir()
        path = directory / f"stammformen-backup-{stamp}.json"
        try:
            atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
        except OSError as exc:
            raise CommandError(f"Couldn't write the backup to {directory}: {exc.strerror or exc}") from exc
        return {"path": str(path), "verbs": len(payload["verbs"]), "results": len(payload.get("results", []))}

    # ------------------------------------------------------------------
    # Import (preview, then commit)
    # ------------------------------------------------------------------
    def _remember(self, preview: Preview) -> str:
        with self.lock:
            pid = f"p{int(self.clock() * 1000):x}{len(self.previews)}"
            self.previews[pid] = preview
            while len(self.previews) > MAX_PREVIEWS:
                self.previews.popitem(last=False)
            return pid

    def _preview_rows(self, rows: list[dict], defaults: dict, issues_in: Optional[list[list[dict]]] = None,
                      weak: bool = False) -> tuple[list[dict], list[dict]]:
        """(UI rows, orig info). Each UI row carries its own validation
        issues and whether the same verb is already in the list."""
        existing = {L.verb_signature(v, weak) for v in self.store.snapshot()["verbs"]}
        ui_rows: list[dict] = []
        orig: list[dict] = []
        for i, raw in enumerate(rows):
            if issues_in is not None:
                clean, issues = raw, issues_in[i]
            else:
                clean, issues = L.normalize_verb(raw, defaults)
            ui_rows.append({
                "cid": f"r{i}",
                **clean,
                "issues": issues,
                "duplicate": L.verb_signature(clean, weak) in existing,
            })
            orig.append({
                "id": raw.get("id") if isinstance(raw, dict) and L.valid_id(raw.get("id")) else None,
                "group": raw.get("group") if isinstance(raw, dict) and isinstance(raw.get("group"), str) else None,
                "origin": raw.get("origin") if isinstance(raw, dict) else None,
                "added_at": raw.get("added_at") if isinstance(raw, dict) else None,
            })
        return ui_rows, orig

    @staticmethod
    def _counts(ui_rows: list[dict]) -> dict:
        lessons = [r["lec"] for r in ui_rows]
        return {
            "rows": len(ui_rows),
            "errors": sum(1 for r in ui_rows if L.has_errors(r["issues"])),
            "warnings": sum(1 for r in ui_rows if any(i["level"] == "warning" for i in r["issues"]) and not L.has_errors(r["issues"])),
            "duplicates": sum(1 for r in ui_rows if r["duplicate"]),
            "min_lec": min(lessons) if lessons else None,
            "max_lec": max(lessons) if lessons else None,
        }

    def cmd_parse_import(self, cmd: dict, progress) -> dict:
        text = cmd.get("text")
        if not isinstance(text, str):
            raise CommandError("There is nothing to import.")
        try:
            parsed = L.detect_and_parse(text, L.clean_text(cmd.get("name"), 200))
        except (ValueError, RecursionError) as exc:
            raise CommandError(str(exc)) from exc
        kind = parsed["kind"]
        if len(parsed["rows"]) > L.MAX_ROWS_PER_IMPORT:
            raise CommandError(f"That list has {len(parsed['rows'])} verbs; the limit is {L.MAX_ROWS_PER_IMPORT} per import.")
        defaults = {"cat": L.DEFAULT_CAT_PASTE, "lec": 1} if kind == "paste" else {"lec": 1}
        ui_rows, orig = self._preview_rows(parsed["rows"], defaults, weak=kind == "paste")
        extras = {k: parsed.get(k) for k in ("groups", "excluded_lessons", "excluded_groups", "mastered", "results") if k in parsed}
        preview = Preview(kind=kind, title=parsed["title"], rows=ui_rows, orig=orig, extras={**extras, "has_progress": parsed["has_progress"]})
        pid = self._remember(preview)
        return {
            "preview_id": pid,
            "kind": kind,
            "title": parsed["title"],
            "rows": ui_rows,
            "problems": parsed["problems"],
            "counts": self._counts(ui_rows),
            "has_progress": parsed["has_progress"],
            "result_count": len(parsed["results"]) if isinstance(parsed.get("results"), list) else 0,
            "existing_verbs": len(self.store.snapshot()["verbs"]),
        }

    def cmd_validate_rows(self, cmd: dict, progress) -> dict:
        """Live validation of an edited preview table: the UI sends the rows
        it is showing and gets back each row's issues and whether the verb
        is already in the list -- so the rules exist in exactly one place."""
        rows = cmd.get("rows")
        if not isinstance(rows, list):
            raise CommandError("Nothing to validate.")
        weak = bool(cmd.get("weak"))
        existing = {L.verb_signature(v, weak) for v in self.store.snapshot()["verbs"]}
        out: dict[str, dict] = {}
        for item in rows[:L.MAX_ROWS_PER_IMPORT]:
            if not isinstance(item, dict) or not isinstance(item.get("cid"), str):
                continue
            clean, issues = L.normalize_verb(item, {"lec": 1})
            out[item["cid"]] = {"issues": issues, "duplicate": L.verb_signature(clean, weak) in existing}
        return {"rows": out}

    def cmd_import_commit(self, cmd: dict, progress) -> dict:
        with self.lock:
            preview = self.previews.get(self._str(cmd, "preview_id"))
        if preview is None:
            raise CommandError("This import has expired -- choose the file again.")
        mode = cmd.get("mode")
        kind = preview.kind
        if kind in ("paste", "scan"):
            mode = "add"
        elif mode not in ("merge", "replace"):
            raise CommandError("Choose whether to add to your list or replace it.")

        chosen = self._chosen_rows(preview, cmd.get("rows"))
        if not chosen:
            raise CommandError("There are no verbs selected to import.")
        bad = [c for c in chosen if L.has_errors(c["issues"])]
        if bad:
            raise CommandError(f"{len(bad)} verb(s) still have problems -- fix or remove them first.")

        title = L.clean_text(cmd.get("title"), L.MAX_TITLE) or preview.title

        if mode == "add":
            origin = "scan" if kind == "scan" else "paste"
            group_name = L.clean_text(cmd.get("group_name"), L.MAX_GROUP_NAME) or ("Scanned page" if kind == "scan" else "Pasted list")
            ids = self.store.add_verbs([c["clean"] for c in chosen], origin, group_name)
            if not self.store.get_title() and title:
                self.store.set_title(title)
            return {"added": len(ids), "skipped": 0}

        if mode == "merge":
            snap = self.store.snapshot()
            have_ids = {v["id"] for v in snap["verbs"]}
            have_sigs = {L.verb_signature(v) for v in snap["verbs"]}
            fresh = []
            skipped = 0
            for c in chosen:
                if (c["id"] and c["id"] in have_ids) or L.verb_signature(c["clean"]) in have_sigs:
                    skipped += 1
                    continue
                fresh.append(c)
            added = self._add_with_groups(fresh, preview)
            self.store.import_lesson_groups(preview.extras.get("groups") or [])
            if not self.store.get_title() and title:
                self.store.set_title(title)
            return {"added": added, "skipped": skipped}

        # replace
        taken: set[str] = set()
        verbs = []
        for c in chosen:
            vid = c["id"] if c["id"] and c["id"] not in taken else L.new_id("v", taken)
            taken.add(vid)
            verbs.append({**c["clean"], "id": vid, "group": c["group"], "origin": c["origin"] if c["origin"] in ("import", "paste", "scan", "manual") else "import",
                          "added_at": c["added_at"]})
        keep_progress = bool(preview.extras.get("has_progress"))
        payload = {
            "title": title,
            "verbs": verbs,
            "groups": preview.extras.get("groups") or [],
            "excluded_lessons": preview.extras.get("excluded_lessons") if keep_progress else [],
            "excluded_groups": preview.extras.get("excluded_groups") if keep_progress else [],
            "mastered": preview.extras.get("mastered") if keep_progress else {},
        }
        self.store.replace_all(payload)
        results = preview.extras.get("results")
        wrote = self.store.replace_results(results) if isinstance(results, list) else None
        return {"added": len(verbs), "skipped": 0, "results": wrote}

    def _chosen_rows(self, preview: Preview, sent: Any) -> list[dict]:
        """The rows to import: the UI's edited rows (matched to the preview
        by cid; `include: false` drops one), each re-validated here."""
        if not isinstance(sent, list):
            sent = [{"cid": r["cid"], "include": not r["duplicate"] or preview.kind in ("html", "list", "backup")} for r in preview.rows]
        by_cid = {r["cid"]: i for i, r in enumerate(preview.rows)}
        out: list[dict] = []
        for item in sent:
            if not isinstance(item, dict) or item.get("include") is False:
                continue
            i = by_cid.get(item.get("cid"))
            if i is None:
                continue
            base = preview.rows[i]
            fields = {k: item[k] if k in item else base[k] for k in ("inf", "conj", "pres", "perf", "ppp", "meaning", "lec", "cat", "bold")}
            clean, issues = L.normalize_verb(fields, {"lec": 1})
            o = preview.orig[i]
            out.append({"cid": base["cid"], "clean": clean, "issues": issues, "id": o["id"], "group": o["group"],
                        "origin": o["origin"], "added_at": o["added_at"]})
        return out

    def _add_with_groups(self, rows: list[dict], preview: Preview) -> int:
        """Adds merge rows. A group a row references in the file is created
        (keeping its id when unused) so a restored list keeps its custom
        groups; a row whose group is unknown joins no group."""
        if not rows:
            return 0
        file_groups = {g.get("id"): g for g in (preview.extras.get("groups") or []) if isinstance(g, dict) and g.get("kind") != "lessons"}
        have_group_ids = {g["id"] for g in self.store.snapshot()["groups"]}
        by_group: dict[Optional[str], list[dict]] = {}
        for r in rows:
            gid = r["group"] if (r["group"] in file_groups or r["group"] in have_group_ids) else None
            by_group.setdefault(gid, []).append(r)
        added = 0
        for gid, group_rows in by_group.items():
            clean_rows = [r["clean"] for r in group_rows]
            ids = [r["id"] for r in group_rows]
            if gid is None:
                new_ids = self.store.add_verbs(clean_rows, "import", "", ids)
            elif gid in have_group_ids:
                new_ids = self.store.add_verbs(clean_rows, "import", "", ids, existing_group=gid)
            else:
                name = L.clean_text(file_groups[gid].get("name"), L.MAX_GROUP_NAME) or "Imported group"
                new_ids = self.store.add_verbs(clean_rows, "import", name, ids, preferred_group_id=gid)
            added += len(new_ids)
        return added

    # ------------------------------------------------------------------
    # Photo scan
    # ------------------------------------------------------------------
    def cmd_ai_status(self, cmd: dict, progress) -> dict:
        return {"ai": self.ai.status()}

    def cmd_scan(self, cmd: dict, progress) -> dict:
        try:
            mime, raw = L.decode_scan_payload(cmd.get("mime"), cmd.get("data_base64"))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        if not self.scan_lock.acquire(blocking=False):
            raise CommandError("A scan is already running -- wait for it to finish.")
        started = time.perf_counter()
        try:
            return self._scan(cmd, mime, raw, progress, started)
        finally:
            self.scan_lock.release()

    def _scan(self, cmd: dict, mime: str, raw: bytes, progress, started: float) -> dict:
        digest = hashlib.sha256(raw).hexdigest()
        key = f"{digest}:p{L.SCAN_PROMPT_VERSION}"
        width = cmd.get("width") if isinstance(cmd.get("width"), int) else None
        height = cmd.get("height") if isinstance(cmd.get("height"), int) else None

        cached = self.cache.get(key)
        if cached:
            progress({"stage": "cache", "message": "Same page as before -- using the saved result, no AI request."})
            rows = cached["rows"]
            ui_rows, orig = self._preview_rows(rows, {"lec": 1, "cat": L.DEFAULT_CAT_SCAN}, weak=True)
            scan = {"cached": True, "requests": 0, "models": cached.get("models", []), "tokens": {"prompt": 0, "output": 0, "total": 0, "estimated": False},
                    "saved_tokens": cached.get("tokens"), "elapsed_s": round(time.perf_counter() - started, 3), "image_hash": digest[:12]}
            return self._scan_preview(ui_rows, orig, scan)

        status = self.ai.status()
        if status.get("state") not in ("ready", "idle"):  # idle: installed and new enough, started on demand
            raise CommandError(status.get("message") or "The AI spin isn't ready.")

        attachments = [{"mime_type": mime, "data_base64": cmd["data_base64"]}]
        pdf_pages = L.count_pdf_pages(raw) if mime == "application/pdf" else None
        used_models: list[str] = []
        tokens = {"prompt": 0, "output": 0, "total": 0, "estimated": False}
        requests = 0

        def ask(task_hint: str) -> tuple[list[dict], list[list[dict]], Optional[str]]:
            nonlocal requests
            requests += 1
            reply = self.ai.ask(L.SCAN_PROMPT, attachments, task_hint, SCAN_TIMEOUT_S)
            if reply.model and reply.model not in used_models:
                used_models.append(reply.model)
            if reply.error:
                raise CommandError(self._friendly_ai_error(reply.error))
            if reply.usage:
                for k in ("prompt", "output", "total"):
                    tokens[k] += int(reply.usage.get(k) or 0)
            else:
                est = L.estimate_scan_tokens(mime, len(raw), width, height, pdf_pages, len(L.SCAN_PROMPT), len(reply.text or ""))
                for k in ("prompt", "output", "total"):
                    tokens[k] += est[k]
                tokens["estimated"] = True
            return L.parse_scan_reply(reply.text)

        progress({"stage": "ai", "message": "Asking the AI to read the page..."})
        rows, issues, fatal = ask("simple")
        acceptable = fatal is None and L.scan_is_acceptable(rows, issues)
        escalated = False
        escalation_reason = ""
        if not acceptable and status.get("cost_bias", "auto") == "auto":
            progress({"stage": "escalate", "message": "The first answer wasn't usable -- trying the stronger model once."})
            escalated = True
            escalation_reason = self._why_unusable(rows, issues, fatal)
            rows2, issues2, fatal2 = ask("complex")
            if fatal2 is None and (fatal is not None or L.scan_is_acceptable(rows2, issues2) or
                                   sum(1 for i in issues2 if not L.has_errors(i)) > sum(1 for i in issues if not L.has_errors(i))):
                rows, issues, fatal = rows2, issues2, fatal2
                acceptable = L.scan_is_acceptable(rows, issues)
        if fatal is not None or not rows:
            raise CommandError(fatal or "The AI found no verbs on the page. Try a sharper, straighter photo.")

        elapsed = round(time.perf_counter() - started, 3)
        scan = {"cached": False, "requests": requests, "models": used_models, "tokens": tokens, "elapsed_s": elapsed,
                "escalated": escalated, "escalation_reason": escalation_reason, "image_hash": digest[:12], "low_quality": not acceptable}
        if acceptable:
            self.cache.put(key, {"rows": rows, "models": used_models, "tokens": tokens})
        ui_rows, orig = self._preview_rows(rows, {"lec": 1, "cat": L.DEFAULT_CAT_SCAN}, issues_in=issues, weak=True)
        return self._scan_preview(ui_rows, orig, scan)

    @staticmethod
    def _why_unusable(rows: list[dict], issues: list[list[dict]], fatal: Optional[str]) -> str:
        """A short, human-readable reason the first (budget model) answer was not
        good enough -- shown next to the token numbers so a prompt that fails the
        cheap model can be diagnosed without re-asking."""
        if fatal:
            return fatal
        counts: dict[str, int] = {}
        for row_issues in issues:
            for i in row_issues:
                if i["level"] == "error":
                    counts[i["field"]] = counts.get(i["field"], 0) + 1
        bad = sum(1 for row_issues in issues if L.has_errors(row_issues))
        top = ", ".join(f"{f} x{n}" for f, n in sorted(counts.items(), key=lambda kv: -kv[1])[:3])
        return f"{bad} of {len(rows)} rows had problems ({top})"

    def _scan_preview(self, ui_rows: list[dict], orig: list[dict], scan: dict) -> dict:
        # Everything a scan produced is AI output: the present forms in
        # particular are conjugated by the model, not read off the page.
        for r in ui_rows:
            r["issues"].append({"field": "pres", "level": "note", "message": "Conjugated by the AI -- check it."})
        preview = Preview(kind="scan", title="", rows=ui_rows, orig=orig, scan=scan)
        pid = self._remember(preview)
        return {"preview_id": pid, "kind": "scan", "title": "", "rows": ui_rows, "problems": [], "counts": self._counts(ui_rows),
                "has_progress": False, "scan": scan}

    @staticmethod
    def _friendly_ai_error(error: str) -> str:
        low = error.lower()
        if "no gemini api key" in low or "api key set yet" in low:
            return "No AI provider key is set. Set up an AI provider in Settings → AI (pick one and paste your key), then scan again."
        if "api key not valid" in low or "api_key_invalid" in low or "permission_denied" in low or "http 401" in low or "http 403" in low:
            return "The AI provider rejected the API key. Check it in Settings → AI and use Save & test."
        if "429" in low or "quota" in low or "rate" in low and "limit" in low:
            return "The AI provider says the quota/rate limit is used up for now. Wait a minute and try again."
        if "can't take" in low and "input" in low:
            return error
        if "bad attachments" in low:
            return f"The AI spin refused the file ({error})."
        return f"The AI request failed: {error}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _str(cmd: dict, key: str) -> str:
        value = cmd.get(key)
        if not isinstance(value, str) or not value:
            raise CommandError(f"Missing '{key}'.")
        return value

    @staticmethod
    def _raise_first_error(issues: list[dict]) -> None:
        for i in issues:
            if i["level"] == "error":
                raise CommandError(i["message"])


def encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
