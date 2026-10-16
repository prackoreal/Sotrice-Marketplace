"""Persistence for the Stammformen trainer (latin-stammformen).

WHY THIS IS SEPARATE FROM THE SPIN'S FILES: installing or updating a .spin
deletes and re-creates its whole folder (downloaded_plugins/<type>/ -- see
install_plugin_from_spin_bytes in server/src/launcher.rs). Anything the user
owns must therefore live OUTSIDE that folder. It lives in the spin's own
data directory:

    %APPDATA%\\Sotrice\\latin-stammformen\\          (Windows; the same
    per-user base the server uses for registry.json/classes/students.json)
    SOTRICE_LATIN_DIR overrides it entirely -- the env-override idiom
    math_teacher.py's SOTRICE_CLASSES_DIR uses, so an isolated test can never
    write into a real machine's data.

Files in that directory:
    data.json        the user's list: verbs (each with a stored, opaque id),
                     groups, excluded lessons/groups, mastery (by verb id).
                     Written atomically (temp file + fsync + os.replace).
    results.jsonl    append-only log of every Test-mode check (verb id, time,
                     which forms were right/wrong). One JSON object per line;
                     a torn last line is skipped on read. A future study app
                     builds streaks/statistics on it -- there is no UI for it.
    scan-cache.json  validated results of photo scans by image hash, so the
                     same photo is never billed twice.
    backups/         copies of data.json taken automatically (first save of
                     each day, before a replace-import, before a schema
                     migration). The newest MAX_BACKUPS are kept.

Schema versioning: data.json carries `schema`. Older files are migrated (the
original is backed up first); a file written by a NEWER version is loaded
read-only and never rewritten, so rolling an update back cannot destroy data.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import latin_logic as L

DATA_FILE = "data.json"
RESULTS_FILE = "results.jsonl"
CACHE_FILE = "scan-cache.json"
BACKUP_DIR = "backups"
MAX_BACKUPS = 30
MAX_CACHE_ENTRIES = 60
RESULT_SCHEMA = 1


class StoreError(Exception):
    """A user-readable problem with a requested change."""


class ReadOnlyError(StoreError):
    pass


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def user_data_dir() -> Path:
    """The per-user Sotrice base directory -- identical to the server's own
    `user_data_dir()` (server/src/launcher.rs)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path(".")
        return root / "Sotrice"
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        home = os.environ.get("HOME")
        return (Path(home) / "Library" / "Application Support" if home else Path(".")) / "Sotrice"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "sotrice"
    home = os.environ.get("HOME")
    return (Path(home) / ".local" / "share" if home else Path(".")) / "sotrice"


def data_dir() -> Path:
    override = os.environ.get("SOTRICE_LATIN_DIR")
    return Path(override) if override else user_data_dir() / "latin-stammformen"


# ---------------------------------------------------------------------------
# Atomic file writes
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str) -> None:
    """Writes `text` so a reader (or a crash) sees either the complete old
    file or the complete new one, never a half-written file. Retries the
    final rename, because on Windows an antivirus scanner or OneDrive can
    hold the target for a moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        last: Optional[OSError] = None
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:  # transient lock on Windows
                last = exc
                time.sleep(0.05 * (attempt + 1))
        assert last is not None
        raise last
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def _migrate_0_to_1(data: dict) -> dict:
    """v0 is any file without a `schema` field: verbs without stored ids and
    mastery keyed by infinitive (the shape of the original single-file page's
    own state). Gives every verb an id and re-keys mastery by it."""
    taken: set[str] = set()
    for v in data.get("verbs", []):
        if isinstance(v, dict) and L.valid_id(v.get("id")):
            taken.add(v["id"])
    by_inf: dict[str, list[str]] = {}
    for v in data.get("verbs", []):
        if not isinstance(v, dict):
            continue
        if not L.valid_id(v.get("id")):
            v["id"] = L.new_id("v", taken)
            taken.add(v["id"])
        by_inf.setdefault(str(v.get("inf", "")), []).append(v["id"])
    old = data.get("mastered", {})
    mastered: dict[str, float] = {}
    if isinstance(old, dict):
        for key, flag in old.items():
            if not flag:
                continue
            ids = [key] if key in taken else by_inf.get(key, [])
            for vid in ids:
                mastered[vid] = float(flag) if isinstance(flag, (int, float)) and not isinstance(flag, bool) else 0.0
    data["mastered"] = mastered
    return data


# version N -> function producing version N+1. A future format change adds one
# entry here (and bumps latin_logic.SCHEMA_VERSION); old files and old backups
# then keep loading.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {0: _migrate_0_to_1}


def migrate(data: dict, target: Optional[int] = None) -> dict:
    target = L.SCHEMA_VERSION if target is None else target
    version = data.get("schema", 0)
    if not isinstance(version, int) or version < 0:
        version = 0
    while version < target:
        step = MIGRATIONS.get(version)
        if step is None:
            raise StoreError(f"No migration from data format {version}.")
        data = step(data)
        version += 1
        data["schema"] = version
    return data


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

def _empty_data() -> dict:
    return {
        "schema": L.SCHEMA_VERSION,
        "title": "",
        "verbs": [],
        "groups": [],
        "excluded_lessons": [],
        "excluded_groups": [],
        "mastered": {},
        "created_at": None,
        "updated_at": None,
    }


class Store:
    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(directory)
        self.clock = clock
        self.lock = threading.RLock()
        self.rev = 0
        self.read_only = False
        self.warnings: list[str] = []
        self.data: dict = _empty_data()
        self._last_backup_day: Optional[str] = None
        self._load()

    # -- paths ---------------------------------------------------------------
    @property
    def data_path(self) -> Path:
        return self.dir / DATA_FILE

    @property
    def results_path(self) -> Path:
        return self.dir / RESULTS_FILE

    @property
    def backup_dir(self) -> Path:
        return self.dir / BACKUP_DIR

    # -- loading -------------------------------------------------------------
    def _load(self) -> None:
        path = self.data_path
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("data.json isn't an object")
        except (OSError, ValueError) as exc:
            self._recover(exc)
            return
        schema = raw.get("schema", 0)
        if isinstance(schema, int) and schema > L.SCHEMA_VERSION:
            self.read_only = True
            self.warnings.append(
                f"This data was saved by a newer version of the trainer (format {schema}); this version understands format "
                f"{L.SCHEMA_VERSION}. It is shown read-only and nothing will be written -- update the spin to edit it."
            )
            self.data = self._sanitize(raw)
            return
        migrated = isinstance(schema, int) and schema < L.SCHEMA_VERSION
        if migrated:
            self._backup(f"before-migration-v{schema}")
            try:
                raw = migrate(raw)
            except StoreError as exc:
                self.read_only = True
                self.warnings.append(f"Your data couldn't be upgraded ({exc}); it is shown read-only and left untouched.")
                self.data = self._sanitize(raw)
                return
        self.data = self._sanitize(raw)
        if migrated:
            self._save()

    def _recover(self, exc: Exception) -> None:
        """data.json is unreadable. It is moved aside (never deleted) and the
        newest readable backup is restored; if there is none, start empty."""
        stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
        corrupt = self.data_path.with_name(f"data.corrupt-{stamp}.json")
        try:
            os.replace(self.data_path, corrupt)
        except OSError:
            pass
        for backup in sorted(self.backup_dir.glob("data-*.json"), reverse=True) if self.backup_dir.exists() else []:
            try:
                raw = json.loads(backup.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("schema", 0), int) and raw.get("schema", 0) <= L.SCHEMA_VERSION:
                    self.data = self._sanitize(migrate(raw))
                    self.warnings.append(
                        f"Your data file couldn't be read ({exc}); it was kept as {corrupt.name} and your list was restored from the backup {backup.name}."
                    )
                    self._save()
                    return
            except (OSError, ValueError, StoreError):
                continue
        self.warnings.append(f"Your data file couldn't be read ({exc}) and there is no backup; it was kept as {corrupt.name}.")

    def _sanitize(self, raw: dict) -> dict:
        """Builds the canonical in-memory structure from a loaded dict. Types
        are repaired, nothing the user typed is dropped (unknown conj values
        are kept as text), duplicate/missing ids are re-issued."""
        data = _empty_data()
        data["title"] = L.clean_text(raw.get("title"), L.MAX_TITLE)
        created = raw.get("created_at")
        data["created_at"] = created if isinstance(created, (int, float)) and not isinstance(created, bool) else None
        updated = raw.get("updated_at")
        data["updated_at"] = updated if isinstance(updated, (int, float)) and not isinstance(updated, bool) else None

        group_ids: set[str] = set()
        for g in raw.get("groups", []) if isinstance(raw.get("groups"), list) else []:
            if not isinstance(g, dict) or not L.valid_id(g.get("id")) or g["id"] in group_ids:
                continue
            name = L.clean_text(g.get("name"), L.MAX_GROUP_NAME) or "Group"
            if g.get("kind") == "lessons":
                lo, hi = L.to_lesson(g.get("min_lec")), L.to_lesson(g.get("max_lec"))
                if lo is None or hi is None:
                    continue
                if lo > hi:
                    lo, hi = hi, lo
                data["groups"].append({"id": g["id"], "name": name, "kind": "lessons", "min_lec": lo, "max_lec": hi,
                                       "created_at": g.get("created_at")})
            else:
                data["groups"].append({"id": g["id"], "name": name, "kind": "list", "created_at": g.get("created_at")})
            group_ids.add(g["id"])

        seen: set[str] = set()
        for v in raw.get("verbs", []) if isinstance(raw.get("verbs"), list) else []:
            if not isinstance(v, dict):
                continue
            vid = v.get("id")
            if not L.valid_id(vid) or vid in seen:
                vid = L.new_id("v", seen)
                self.warnings.append("A verb without a usable id was found and given a new one.")
            seen.add(vid)
            lec = L.to_lesson(v.get("lec"))
            group = v.get("group") if isinstance(v.get("group"), str) and v.get("group") in group_ids else None
            origin = v.get("origin") if v.get("origin") in ("import", "paste", "scan", "manual") else "import"
            data["verbs"].append({
                "id": vid,
                "inf": L.clean_text(v.get("inf"), L.MAX_INF),
                "conj": L.normalize_conj(v.get("conj")) or L.clean_text(v.get("conj"), 12),
                "pres": L.clean_text(v.get("pres"), L.MAX_PRES),
                "perf": L.clean_text(v.get("perf"), L.MAX_PERF),
                "ppp": L.normalize_ppp(v.get("ppp")),
                "meaning": L.clean_text(v.get("meaning"), L.MAX_MEANING),
                "lec": 1 if lec is None else lec,
                "cat": L.clean_text(v.get("cat"), L.MAX_CAT),
                "bold": L.to_bool(v.get("bold")),
                "group": group,
                "origin": origin,
                "added_at": v.get("added_at") if isinstance(v.get("added_at"), (int, float)) and not isinstance(v.get("added_at"), bool) else None,
            })

        lessons = raw.get("excluded_lessons")
        data["excluded_lessons"] = sorted({n for n in (L.to_lesson(x) for x in lessons) if n is not None}) if isinstance(lessons, list) else []
        groups = raw.get("excluded_groups")
        data["excluded_groups"] = sorted({g for g in groups if isinstance(g, str) and g in group_ids}) if isinstance(groups, list) else []
        mastered = raw.get("mastered")
        if isinstance(mastered, dict):
            # Presence means mastered; the value is only when (0.0 = unknown, e.g. a migrated flag).
            data["mastered"] = {k: (float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else 0.0)
                                for k, t in mastered.items() if k in seen and t is not False and t is not None}
        return data

    # -- saving --------------------------------------------------------------
    def _require_writable(self) -> None:
        if self.read_only:
            raise ReadOnlyError("This data is read-only (it was saved by a newer version). Update the spin to change it.")

    def _save(self) -> None:
        with self.lock:
            now = self.clock()
            if self.data["created_at"] is None:
                self.data["created_at"] = now
            self.data["updated_at"] = now
            self.data["schema"] = L.SCHEMA_VERSION
            self._maybe_daily_backup()
            atomic_write_json(self.data_path, self.data)
            self.rev += 1

    def _backup(self, reason: str) -> Optional[Path]:
        """Copies the current data.json into backups/ (no-op if there is no
        file yet). A failed backup is logged, never fatal."""
        try:
            if not self.data_path.exists():
                return None
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
            target = self.backup_dir / f"data-{stamp}-{reason}.json"
            n = 1
            while target.exists():
                n += 1
                target = self.backup_dir / f"data-{stamp}-{reason}-{n}.json"
            shutil.copy2(self.data_path, target)
            self._prune_backups()
            return target
        except OSError as exc:
            print(f"[latin-stammformen] couldn't write a backup: {exc!r}", flush=True)
            return None

    def _maybe_daily_backup(self) -> None:
        day = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d")
        if self._last_backup_day == day:
            return
        if self.backup_dir.exists() and any(self.backup_dir.glob(f"data-{day}-*.json")):
            self._last_backup_day = day
            return
        # Marked done only once a backup exists; the very first save has no file
        # to copy yet, so the SECOND change of the day takes the day's backup.
        if self._backup("auto") is not None:
            self._last_backup_day = day

    def _prune_backups(self) -> None:
        files = sorted(self.backup_dir.glob("data-*.json"))
        # Pre-migration copies are the user's way back from a bad upgrade: never pruned.
        prunable = [f for f in files if "before-migration" not in f.name]
        for old in prunable[:-MAX_BACKUPS] if len(prunable) > MAX_BACKUPS else []:
            try:
                old.unlink()
            except OSError:
                pass

    # -- reading -------------------------------------------------------------
    def snapshot(self) -> dict:
        """Everything the UI needs, as fresh plain dicts (safe to serialise
        on another thread). Mastery is by verb id; `kind` is the derived
        conjugation-filter value (see latin_logic.conj_kind)."""
        with self.lock:
            d = self.data
            verbs = []
            for v in d["verbs"]:
                item = dict(v)
                item["kind"] = L.conj_kind(v)
                verbs.append(item)
            return {
                "rev": self.rev,
                "schema": d["schema"],
                "title": d["title"],
                "verbs": verbs,
                "groups": [dict(g) for g in d["groups"]],
                "excluded_lessons": list(d["excluded_lessons"]),
                "excluded_groups": list(d["excluded_groups"]),
                "mastered": dict(d["mastered"]),
                "read_only": self.read_only,
                "warnings": list(self.warnings),
                "data_dir": str(self.dir),
            }

    def verb(self, verb_id: str) -> Optional[dict]:
        with self.lock:
            for v in self.data["verbs"]:
                if v["id"] == verb_id:
                    return dict(v)
        return None

    def is_empty(self) -> bool:
        with self.lock:
            return not self.data["verbs"]

    # -- mutations (each validates, then persists atomically) ----------------
    def _taken_verb_ids(self) -> set[str]:
        return {v["id"] for v in self.data["verbs"]}

    def _taken_group_ids(self) -> set[str]:
        return {g["id"] for g in self.data["groups"]}

    def add_verbs(self, rows: list[dict], origin: str, group_name: str = "", ids: Optional[list[Optional[str]]] = None,
                  existing_group: Optional[str] = None, preferred_group_id: Optional[str] = None) -> list[str]:
        """Adds already-validated rows (fields as returned by
        L.normalize_verb, no errors). Puts them into `existing_group`, or
        creates one list group when `group_name` is given (keeping
        `preferred_group_id` if it is valid and unused). `ids`, if given, are
        preferred verb ids (kept when valid and unused). Returns the new
        verbs' ids."""
        with self.lock:
            self._require_writable()
            now = self.clock()
            group_id = None
            if existing_group is not None:
                if existing_group not in self._taken_group_ids():
                    raise StoreError("That group no longer exists.")
                group_id = existing_group
            elif group_name:
                taken_groups = self._taken_group_ids()
                group_id = preferred_group_id if L.valid_id(preferred_group_id) and preferred_group_id not in taken_groups else L.new_id("g", taken_groups)
                self.data["groups"].append({"id": group_id, "name": L.clean_text(group_name, L.MAX_GROUP_NAME) or "New group",
                                            "kind": "list", "created_at": now})
            taken = self._taken_verb_ids()
            new_ids: list[str] = []
            for i, row in enumerate(rows):
                preferred = ids[i] if ids and i < len(ids) else None
                vid = preferred if L.valid_id(preferred) and preferred not in taken else L.new_id("v", taken)
                taken.add(vid)
                self.data["verbs"].append({**self._verb_fields(row), "id": vid, "group": group_id, "origin": origin, "added_at": now})
                new_ids.append(vid)
            self._save()
            return new_ids

    def import_lesson_groups(self, groups: list) -> int:
        """Adds lesson-range groups (from a backup) whose id isn't present."""
        with self.lock:
            self._require_writable()
            known = self._taken_group_ids()
            added = 0
            for g in groups:
                if not isinstance(g, dict) or g.get("kind") != "lessons" or not L.valid_id(g.get("id")) or g["id"] in known:
                    continue
                lo, hi = L.to_lesson(g.get("min_lec")), L.to_lesson(g.get("max_lec"))
                if lo is None or hi is None:
                    continue
                if lo > hi:
                    lo, hi = hi, lo
                self.data["groups"].append({"id": g["id"], "name": L.clean_text(g.get("name"), L.MAX_GROUP_NAME) or "Custom chapters",
                                            "kind": "lessons", "min_lec": lo, "max_lec": hi, "created_at": self.clock()})
                known.add(g["id"])
                added += 1
            if added:
                self._save()
            return added

    def get_title(self) -> str:
        with self.lock:
            return self.data["title"]

    @staticmethod
    def _verb_fields(row: dict) -> dict:
        return {k: row[k] for k in ("inf", "conj", "pres", "perf", "ppp", "meaning", "lec", "cat", "bold")}

    def update_verb(self, verb_id: str, fields: dict) -> None:
        with self.lock:
            self._require_writable()
            for v in self.data["verbs"]:
                if v["id"] == verb_id:
                    merged = {**{k: v[k] for k in ("inf", "conj", "pres", "perf", "ppp", "meaning", "lec", "cat", "bold")}, **fields}
                    clean, issues = L.normalize_verb(merged)
                    errors = [i for i in issues if i["level"] == "error"]
                    if errors:
                        raise StoreError(errors[0]["message"])
                    v.update(clean)
                    self._save()
                    return
            raise StoreError("That verb no longer exists.")

    def delete_verb(self, verb_id: str) -> None:
        with self.lock:
            self._require_writable()
            before = len(self.data["verbs"])
            self.data["verbs"] = [v for v in self.data["verbs"] if v["id"] != verb_id]
            if len(self.data["verbs"]) == before:
                raise StoreError("That verb no longer exists.")
            self.data["mastered"].pop(verb_id, None)
            self._save()

    def set_mastered(self, verb_id: str, flag: bool) -> None:
        with self.lock:
            self._require_writable()
            if verb_id not in self._taken_verb_ids():
                raise StoreError("That verb no longer exists.")
            if flag:
                self.data["mastered"].setdefault(verb_id, self.clock())
            else:
                self.data["mastered"].pop(verb_id, None)
            self._save()

    def set_excluded(self, lessons: dict[int, bool], groups: dict[str, bool]) -> None:
        """Idempotent deltas: lesson/group -> excluded?"""
        with self.lock:
            self._require_writable()
            ex_l = set(self.data["excluded_lessons"])
            for lec, flag in lessons.items():
                (ex_l.add if flag else ex_l.discard)(lec)
            ex_g = set(self.data["excluded_groups"])
            known = self._taken_group_ids()
            for gid, flag in groups.items():
                if gid not in known:
                    continue
                (ex_g.add if flag else ex_g.discard)(gid)
            self.data["excluded_lessons"] = sorted(ex_l)
            self.data["excluded_groups"] = sorted(ex_g)
            self._save()

    def create_lesson_group(self, name: str, min_lec: int, max_lec: int) -> str:
        with self.lock:
            self._require_writable()
            lo, hi = L.to_lesson(min_lec), L.to_lesson(max_lec)
            if lo is None or hi is None:
                raise StoreError("Enter two lesson numbers.")
            if lo > hi:
                lo, hi = hi, lo
            gid = L.new_id("g", self._taken_group_ids())
            self.data["groups"].append({"id": gid, "name": L.clean_text(name, L.MAX_GROUP_NAME) or "Custom chapters",
                                        "kind": "lessons", "min_lec": lo, "max_lec": hi, "created_at": self.clock()})
            self._save()
            return gid

    def delete_group(self, group_id: str, delete_verbs: bool) -> int:
        """Removes a user group. For a list group its verbs are either kept
        (they rejoin the lesson chapters) or deleted too. Returns how many
        verbs were deleted."""
        with self.lock:
            self._require_writable()
            if group_id not in self._taken_group_ids():
                raise StoreError("That group no longer exists.")
            deleted = 0
            if delete_verbs:
                gone = {v["id"] for v in self.data["verbs"] if v.get("group") == group_id}
                deleted = len(gone)
                self.data["verbs"] = [v for v in self.data["verbs"] if v["id"] not in gone]
                for vid in gone:
                    self.data["mastered"].pop(vid, None)
            else:
                for v in self.data["verbs"]:
                    if v.get("group") == group_id:
                        v["group"] = None
            self.data["groups"] = [g for g in self.data["groups"] if g["id"] != group_id]
            self.data["excluded_groups"] = [g for g in self.data["excluded_groups"] if g != group_id]
            self._save()
            return deleted

    def set_title(self, title: str) -> None:
        with self.lock:
            self._require_writable()
            self.data["title"] = L.clean_text(title, L.MAX_TITLE)
            self._save()

    def replace_all(self, payload: dict) -> None:
        """Replaces the whole list with `payload` ({title, verbs (each with
        id), groups, excluded_*, mastered}); the previous data.json is
        backed up first. `payload` has been validated by the caller."""
        with self.lock:
            self._require_writable()
            self._backup("before-replace")
            self.data = self._sanitize({**payload, "schema": L.SCHEMA_VERSION,
                                        "created_at": self.data.get("created_at") or self.clock()})
            self._save()

    # -- results log ---------------------------------------------------------
    def append_result(self, verb_id: str, ok: dict, given: dict, empty: bool) -> None:
        """Appends one Test-mode check to results.jsonl. Never raises: a
        log write that fails must not break studying."""
        if self.read_only:
            return
        entry = {
            "v": RESULT_SCHEMA,
            "t": round(self.clock(), 3),
            "verb_id": verb_id,
            "mode": "test",
            "ok": {k: bool(ok.get(k)) for k in ("pres", "perf", "ppp", "meaning")},
            "all_ok": all(bool(ok.get(k)) for k in ("pres", "perf", "ppp", "meaning")),
            "empty": bool(empty),
            "given": {k: L.clean_text(given.get(k), 200) for k in ("pres", "perf", "ppp", "meaning")},
        }
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self.lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                path = self.results_path
                needs_newline = False
                if path.exists() and path.stat().st_size > 0:
                    with open(path, "rb") as f:
                        f.seek(-1, os.SEEK_END)
                        needs_newline = f.read(1) != b"\n"
                with open(path, "a", encoding="utf-8", newline="\n") as f:
                    if needs_newline:
                        f.write("\n")
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
        except OSError as exc:
            print(f"[latin-stammformen] couldn't log a result: {exc!r}", flush=True)

    def read_results(self) -> list[dict]:
        out: list[dict] = []
        try:
            text = self.results_path.read_text(encoding="utf-8")
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a torn or damaged line: skip it, keep the rest
            if isinstance(item, dict) and isinstance(item.get("verb_id"), str):
                out.append(item)
        return out

    def replace_results(self, results: list) -> int:
        """Replaces results.jsonl with `results` (validated and normalised);
        the old log is kept in backups/. Returns how many were written."""
        with self.lock:
            self._require_writable()
            clean: list[str] = []
            for item in results:
                if not isinstance(item, dict) or not isinstance(item.get("verb_id"), str):
                    continue
                t = item.get("t")
                ok = item.get("ok") if isinstance(item.get("ok"), dict) else {}
                given = item.get("given") if isinstance(item.get("given"), dict) else {}
                entry = {
                    "v": RESULT_SCHEMA,
                    "t": float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else 0.0,
                    "verb_id": item["verb_id"][:40],
                    "mode": "test",
                    "ok": {k: bool(ok.get(k)) for k in ("pres", "perf", "ppp", "meaning")},
                    "all_ok": all(bool(ok.get(k)) for k in ("pres", "perf", "ppp", "meaning")),
                    "empty": bool(item.get("empty", False)),
                    "given": {k: L.clean_text(given.get(k), 200) for k in ("pres", "perf", "ppp", "meaning")},
                }
                clean.append(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
            if self.results_path.exists():
                try:
                    self.backup_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.fromtimestamp(self.clock()).strftime("%Y%m%d-%H%M%S")
                    shutil.copy2(self.results_path, self.backup_dir / f"results-{stamp}.jsonl")
                except OSError as exc:
                    print(f"[latin-stammformen] couldn't back up the results log: {exc!r}", flush=True)
            atomic_write_text(self.results_path, "".join(line + "\n" for line in clean))
            return len(clean)

    # -- export --------------------------------------------------------------
    def export_payload(self, include_results: bool = True, app_version: str = "") -> dict:
        with self.lock:
            d = self.data
            payload = {
                "format": L.FORMAT_NAME,
                "schema": L.SCHEMA_VERSION,
                "exported_at": datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec="seconds"),
                "exported_by": f"latin-stammformen {app_version}".strip(),
                "title": d["title"],
                "verbs": [dict(v) for v in d["verbs"]],
                "groups": [dict(g) for g in d["groups"]],
                "excluded_lessons": list(d["excluded_lessons"]),
                "excluded_groups": list(d["excluded_groups"]),
                "mastered": dict(d["mastered"]),
            }
        if include_results:
            payload["results"] = self.read_results()
        return payload


# ---------------------------------------------------------------------------
# Scan cache
# ---------------------------------------------------------------------------

class ScanCache:
    """image-hash -> validated scan result. Only validated verb rows and
    bookkeeping are stored, never the image."""

    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(directory) / CACHE_FILE
        self.clock = clock
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                self.entries = {k: v for k, v in raw["entries"].items() if isinstance(v, dict) and isinstance(v.get("rows"), list)}
        except (OSError, ValueError):
            self.entries = {}

    def get(self, key: str) -> Optional[dict]:
        with self.lock:
            entry = self.entries.get(key)
            return json.loads(json.dumps(entry)) if entry else None

    def put(self, key: str, entry: dict) -> None:
        with self.lock:
            self.entries[key] = {**entry, "at": self.clock()}
            if len(self.entries) > MAX_CACHE_ENTRIES:
                for old in sorted(self.entries, key=lambda k: self.entries[k].get("at", 0))[: len(self.entries) - MAX_CACHE_ENTRIES]:
                    del self.entries[old]
            try:
                atomic_write_json(self.path, {"schema": 1, "entries": self.entries})
            except OSError as exc:  # a cache that can't be written costs money next time, never correctness now
                print(f"[latin-stammformen] couldn't write the scan cache: {exc!r}", flush=True)
