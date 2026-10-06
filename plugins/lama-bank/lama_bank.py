"""LaMa Bank — a data-holding companion plugin for math-teacher, same
relationship city-weather has to city-layout: it doesn't run a class or
render anything itself, it just holds real imported content and exposes
it over Core so something else (today: a raw authoring_command, same
idiom this file's own commands use; eventually: a real browse/search UI
inside math-teacher's own bundled UI, once the plugin-UI dashboard port
lands — see clients/python/lama_parser.py's own module docstring and
this repo's plugin-UI-architecture memory) can pick real content and
file it into a real class's bank.

Deliberately NOT a UI of any kind — building one now, bolted onto
Sotrice's own pre-architecture-fix app code, would repeat the exact
mistake the ongoing plugin-UI migration exists to undo. This plugin's
whole job is the DATA layer: scan real LaMa PDFs (via lama_parser.py,
already built and verified separately), hold what it finds, publish it
browsably, and — on request — file a chosen subset into a specific
running math-teacher class's own bank through the exact same
authoring_command "import_questions" channel a human-driven import
already uses. Nothing here bypasses or duplicates math_teacher.py's own
validation; every question this plugin sends still goes through
validate_question_content() there, same as any other caller.

Where the PDFs come from: a watch folder, LAMA_PDF_DIR (or, if unset,
a `lama_pdfs/` folder next to this script, created on demand) — same
env-override idiom sotrice-server's own SOTRICE_PLUGIN_PORT/SOTRICE_WS_PORT
and math_teacher.py's own SOTRICE_CLASSES_DIR already use, so a real
teacher just points this at wherever their own LaMa exports live
without editing source. Nothing about a specific person's own folder
layout belongs in committed code.

AI-assisted parsing fallback: every scan auto-starts "ai-test" (see
plugins/registry.json and clients/python/ai_test.py — the universal,
domain-agnostic ai_request/ai_response mechanism) if it isn't already
running (checked via list_running() first — start_plugin has no dedup
of its own), and hands lama_parser.py's parse_lama_pdf an ai_fallback
callable that bridges to it (a dedicated entity, one outstanding
ai_request at a time, matched by a fresh request_id per call — see
ai_fallback()'s own comment). This plugin has no idea HOW ai-test
answers, same domain-agnostic posture as everything else that mechanism
is meant to serve. Strictly a FALLBACK, tried only after
lama_parser.py's own geometry/color-based parsing already failed for a
SUPPORTED Aufgabenformat — never a replacement for it, and never tried
at all for a genuinely unsupported format (Lückentext, BIFIE Oberstufe,
...), which would need a whole new question TYPE, not just better
extraction into an existing one. Every AI-recovered question is flagged
in its own review_reason ("AI-assisted extraction (Gemini) ...") so a
teacher never mistakes it for the geometry parser's own high-confidence
result.

Entities published:
  A single control entity, kind="lama_bank":
    lama_catalog (list[{"id", "titel", "grundkompetenz", "klasse",
      "quelle", "aufgabenformat", "type", "review_reason",
      "source_pdf"}]) — METADATA ONLY, safe to browse/search/filter
      freely; deliberately excludes the actual question content
      (options/pairs/accepted_answers/points) so this stays a small,
      cheap-to-push list even with thousands of catalog entries. The
      full content for a chosen id is only ever read internally by this
      plugin itself, at the moment it's actually filed into a class
      (see "import_into_class" below) — never published wholesale.
    lama_skipped (list[{"titel", "grundkompetenz", "klasse", "quelle",
      "aufgabenformat", "reason"}]) — every exercise lama_parser.py
      couldn't turn into any question at all, with why, so a teacher
      can see the real gaps (Lückentext, BIFIE essay content, ...)
      instead of a silently smaller catalog than they expected.
    lama_scan_status ({"pdf_count", "imported_count", "skipped_count",
      "scanned_at", "cancelled"}) — a glance-able summary, republished
      after every scan (startup, any "rescan"/"upload_pdf", or a scan a
      "cancel_scan" command cut short — "cancelled" is true only in that
      last case, with whatever was found before the cancel landed).
    lama_scan_progress ({"file_index", "file_total", "filename",
      "exercise_done", "exercise_total"}) — republished once per
      EXERCISE within the file currently being scanned (not just once
      per file), so a UI can show real progress on a large watch folder
      OR a single large PDF (many exercises, some possibly slow due to
      an AI-fallback round trip) instead of a frozen screen either way.
      Not meant to be durable state on its own — a client tells a scan
      is over by lama_catalog/lama_scan_status arriving (both always
      republished right after the last progress tick), not by any field
      on this attribute itself.
    lama_recent_uploads (list[{"filename", "uploaded_at"}]) — up to
      MAX_RECENT_UPLOADS *.pdf files physically in WATCH_DIR right now,
      newest mtime first. Republished alongside lama_catalog/lama_skipped/
      lama_scan_status (startup, any rescan/upload_pdf/select_recent).
      Needs no persistence of its own — WATCH_DIR's own files already
      are the durable record of "what was uploaded" — so this is a thin,
      stateless read of the filesystem, not a tracked log. What a "Recent
      uploads" picker shows so a teacher can re-pick a PDF they (or
      someone) already uploaded without re-browsing their filesystem for
      it — see "select_recent" below.

Reads (bare/unnamespaced, same trust tier as class_code/load_class in
math_teacher.py — this plugin is exactly as loopback-trusted as
anything else that talks to sotrice-server from this machine):
    class_code — every math-teacher instance publishes this bare, the
      same discovery mechanism math_student.py already uses to find a
      class to join; here it's how this plugin finds a class to FILE
      questions INTO. Tracked as {code: control_entity}, last write
      wins for a given code (two live classes sharing a code is
      astronomically unlikely — see math_teacher.py's own
      generate_class_code — and not specially guarded against here for
      the same reason it isn't there).
    lama_command — {"cmd": ..., ...}:
      "upload_pdf" — {"filename": str, "data_base64": str} — a PDF
        picked client-side in math-teacher-ui's own sandboxed iframe,
        which has no filesystem access at all, so its bytes travel here
        base64-encoded over this same attribute channel every other
        command already uses (no message-size limit exists anywhere in
        the WS protocol or Core's attribute storage, confirmed before
        building this — a real worksheet PDF fits fine in one write).
        Decoded and written into WATCH_DIR under its own filename
        (basename-only, defending against a path-traversal filename),
        then handled with the EXACT same scan/parse path "rescan" uses
        below (run on its own thread — see _run_scan_async's own doc
        comment on why a scan can't just run synchronously here) — this
        is not a separate import pipeline. Publishes lama_upload_result
        ({"filename", "ok", "imported_count", "error"?}) once that scan
        finishes, so the UI has real, immediate feedback (a parsed
        catalog subset, or a clear reason it failed) rather than needing
        to guess from lama_catalog/lama_skipped alone. An invalid
        payload (missing fields, non-.pdf filename, bad base64), or a
        scan already running, is answered the same way — a real error,
        never silently dropped.
      "select_recent" — {"filename": str} — re-runs the exact same
        scan/parse pipeline "upload_pdf" does, for a PDF that's already
        sitting in WATCH_DIR (see lama_recent_uploads) instead of a
        freshly-uploaded one — there are no new bytes to write, so this
        skips straight to scanning. Publishes lama_upload_result the same
        way upload_pdf does once that scan finishes. A real, reported
        error (never silent) if `filename` is no longer in the watch
        folder (removed by something other than this plugin since it was
        last listed) or a scan is already running.
      "rescan" — re-scans the watch folder from scratch (new PDFs
        dropped in since startup, or any changed since last scan) and
        republishes lama_catalog/lama_skipped/lama_scan_status. A no-op
        if a scan is already running.
      "cancel_scan" — cuts short whatever scan (a "rescan" or
        "upload_pdf") is currently running, at the next exercise-level
        checkpoint (see lama_scan_progress's own doc comment) — a no-op
        if nothing is running. Whatever the scan found before the cancel
        lands is kept, same as any other point a scan could stop midway
        (MAX_CATALOG_ENTRIES, a corrupt PDF, ...).
      "import_into_class" — {"class_code", "question_ids": [str, ...],
        "chapter_id"? , "new_chapter_name"?} — looks up `class_code`
        among currently-known RUNNING classes (not the persisted
        index — a class that isn't actually running right now has no
        live authoring_command to write to), builds an
        "import_questions" authoring_command from whichever of
        `question_ids` are still in the catalog (an id from a stale
        catalog snapshot silently drops, same "tolerate a few bad
        entries in a real batch" posture math_teacher.py's own
        import_questions already has), and writes it to that class's
        control entity. Capped at MAX_IMPORT_PER_REQUEST per command,
        mirroring math_teacher.py's own bound (duplicated as a plain
        constant rather than imported — importing math_teacher.py
        itself would run its whole `with World() as world:` body and
        try to open a second live connection just to read one number).
        Unknown class_code, or no ids found in the catalog, is a no-op.
    Anything else (missing/unknown "cmd", extra junk keys) is silently
    dropped, same posture as every command handler in this codebase.
"""

import base64
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from lama_parser import METADATA_FIELDS, AiFallback, parse_lama_pdf
from sotrice_client import World

# How long one AI-assisted extraction attempt waits for a real ai_response
# before giving up and falling through to a plain skip -- generous (a
# cold-started ai-test process needs real time to import/read its API
# key before its FIRST answer, and every answer is one real network
# round trip to Gemini), but bounded, since scan_watch_dir() calls this
# synchronously, once per exercise that needs it, in a plain loop -- see
# _ai_fallback's own doc comment on why that's fine for now.
_AI_FALLBACK_TIMEOUT_S = 25.0

WATCH_DIR = Path(os.environ["LAMA_PDF_DIR"]) if os.environ.get("LAMA_PDF_DIR") else Path(__file__).parent / "lama_pdfs"

# Same bound as math_teacher.py's own MAX_IMPORT_PER_REQUEST — see this
# module's own docstring on why it's duplicated rather than imported.
MAX_IMPORT_PER_REQUEST = 200
# Generous for even the largest real LaMa export mentioned (2000+
# exercises), tight enough that a folder someone points this at by
# mistake (a huge, unrelated PDF collection) can't grow this plugin's
# own memory/push size without bound.
MAX_CATALOG_ENTRIES = 5000
# How many lama_recent_uploads entries to publish -- a quick-pick list,
# not a full history; plenty for "the last few files someone uploaded"
# without the push growing unbounded on a watch folder that accumulates
# PDFs for a whole semester.
MAX_RECENT_UPLOADS = 10


def _recent_uploads() -> list[dict]:
    """Up to MAX_RECENT_UPLOADS *.pdf files physically sitting in
    WATCH_DIR right now, newest-written first -- see lama_recent_uploads'
    own doc comment on why this reads the filesystem fresh each time
    rather than tracking a separate persisted log. Safe to call even if
    WATCH_DIR doesn't exist yet (Path.glob on a missing directory just
    yields nothing, same as an empty one)."""
    try:
        pdfs = [(p, p.stat().st_mtime) for p in WATCH_DIR.glob("*.pdf")]
    except OSError:
        return []
    pdfs.sort(key=lambda pair: pair[1], reverse=True)
    return [{"filename": p.name, "uploaded_at": mtime} for p, mtime in pdfs[:MAX_RECENT_UPLOADS]]


def _new_catalog_id(source_pdf: str, index: int) -> str:
    # Stable for the lifetime of one scan (source filename + its
    # position in that file's own imported list) — good enough for a
    # UI to reference an entry within one session; a "rescan" is a full
    # rebuild, so ids aren't guaranteed stable ACROSS scans, same
    # "identity only matters within the current snapshot" posture a
    # browsable catalog like this can afford to have.
    return f"{source_pdf}:{index}"


def scan_watch_dir(
    ai_fallback: Optional[AiFallback] = None,
    on_progress: Optional[Callable[[int, int, str, int, int], bool]] = None,
) -> tuple[dict[str, dict], list[dict], list[dict]]:
    """Parses every *.pdf in WATCH_DIR. Returns (questions_by_id — the
    FULL question content, never published wholesale, see the module
    docstring — catalog_entries, skipped_entries). `ai_fallback`, if
    given, is handed straight through to parse_lama_pdf — see its own
    doc comment for exactly when/why it's tried.

    `on_progress`, if given, is called (file_index, file_total, filename,
    exercise_done, exercise_total) — file_index is the 1-based file
    CURRENTLY being processed (not yet necessarily finished), and
    exercise_done/exercise_total are that file's own real exercise
    progress, threaded straight through to parse_lama_pdf's own
    on_progress (see its doc comment on why per-exercise granularity
    matters: a single large PDF is exactly the case where file-level
    granularity alone shows nothing until the whole thing is done).
    Returning False stops the scan entirely (this file's partial results
    are kept, no further files are opened) — this is the cancellation
    mechanism a "cancel_scan" command uses, see lama_bank.py's own
    on_lama_command."""
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    questions_by_id: dict[str, dict] = {}
    catalog: list[dict] = []
    skipped: list[dict] = []

    pdf_paths = sorted(WATCH_DIR.glob("*.pdf"))
    total = len(pdf_paths)
    cancelled = False
    for file_index, pdf_path in enumerate(pdf_paths, start=1):
        if len(catalog) >= MAX_CATALOG_ENTRIES:
            break

        def _file_progress(ex_done: int, ex_total: int, _file_index: int = file_index, _name: str = pdf_path.name) -> bool:
            nonlocal cancelled
            if on_progress is None:
                return True
            keep_going = on_progress(_file_index, total, _name, ex_done, ex_total)
            if not keep_going:
                cancelled = True
            return keep_going

        try:
            result = parse_lama_pdf(str(pdf_path), ai_fallback=ai_fallback, on_progress=_file_progress)
        except Exception as exc:  # noqa: BLE001 - one bad/corrupt PDF must not take the whole scan down
            skipped.append({
                "titel": pdf_path.name, "grundkompetenz": "", "klasse": "", "quelle": "",
                "aufgabenformat": "", "reason": f"failed to parse this PDF at all: {exc!r}",
            })
            _file_progress(0, 0)
            if cancelled:
                break
            continue

        for i, parsed in enumerate(result.imported):
            if len(catalog) >= MAX_CATALOG_ENTRIES:
                break
            catalog_id = _new_catalog_id(pdf_path.name, i)
            questions_by_id[catalog_id] = parsed.question
            catalog.append({
                "id": catalog_id,
                "titel": parsed.source.get("Titel", ""),
                "grundkompetenz": parsed.source.get("Grundkompetenz(en)", ""),
                "klasse": parsed.source.get("Klasse", ""),
                "quelle": parsed.source.get("Quelle", ""),
                "aufgabenformat": parsed.source.get("Aufgabenformat", ""),
                "type": parsed.question["type"],
                "review_reason": parsed.review_reason,
                "source_pdf": pdf_path.name,
            })

        for sk in result.skipped:
            skipped.append({
                "titel": sk.source.get("Titel", ""),
                "grundkompetenz": sk.source.get("Grundkompetenz(en)", ""),
                "klasse": sk.source.get("Klasse", ""),
                "quelle": sk.source.get("Quelle", ""),
                "aufgabenformat": sk.source.get("Aufgabenformat", ""),
                "reason": sk.reason,
            })

        if cancelled:
            break

    return questions_by_id, catalog, skipped


with World() as world:
    name = world.identify("lama-bank")

    control = world.create_entity()
    world.set_attribute(control, "kind", "lama_bank")

    questions_by_id: dict[str, dict] = {}

    # AI-assisted parsing fallback (see lama_parser.py's own parse_lama_pdf
    # doc comment) -- a second, later caller of the exact same domain-
    # agnostic ai_request/ai_response mechanism math-teacher-ui's AI tab
    # uses (see plugins/registry.json's "ai-test" entry and
    # clients/python/ai_test.py); this plugin has no idea HOW ai-test
    # answers, only that it does. One dedicated entity, reused across
    # calls -- safe because scan_watch_dir() calls ai_fallback()
    # synchronously, one exercise at a time, so there's never more than
    # one outstanding request at once (see _AI_FALLBACK_TIMEOUT_S's own
    # comment on why that's an acceptable tradeoff for now).
    ai_bridge_entity = world.create_entity()
    _ai_pending: dict[str, threading.Event] = {}
    _ai_replies: dict[str, Optional[str]] = {}
    _ai_lock = threading.Lock()

    def _on_ai_response(entity, attribute, value, source):
        if not isinstance(value, dict):
            return
        request_id = value.get("request_id")
        if not isinstance(request_id, str):
            return
        with _ai_lock:
            event = _ai_pending.get(request_id)
            if event is None:
                return  # not one of ours, or already timed out and cleaned up
            _ai_replies[request_id] = value.get("text")
        event.set()

    world.subscribe("ai_response", _on_ai_response, replay=False)

    def _ensure_ai_test_running() -> None:
        try:
            running = world.list_running()
        except Exception as exc:  # noqa: BLE001 - a query failure must not block a scan over a nice-to-have
            print(f"[{name}] couldn't check for a running ai-test instance: {exc!r}")
            return
        if any(r.get("plugin_type") == "ai-test" for r in running):
            return
        try:
            world.start_plugin("ai-test")
        except Exception as exc:  # noqa: BLE001 - AI fallback is optional; its absence just means fewer recovered exercises
            print(f"[{name}] couldn't start ai-test for AI-assisted parsing: {exc!r}")

    def ai_fallback(prompt: str) -> Optional[str]:
        request_id = str(uuid.uuid4())
        event = threading.Event()
        with _ai_lock:
            _ai_pending[request_id] = event
        world.set_attribute(ai_bridge_entity, "ai_request", {"prompt": prompt, "request_id": request_id})
        got_reply = event.wait(_AI_FALLBACK_TIMEOUT_S)
        with _ai_lock:
            _ai_pending.pop(request_id, None)
            text = _ai_replies.pop(request_id, None)
        if not got_reply:
            print(f"[{name}] AI fallback timed out after {_AI_FALLBACK_TIMEOUT_S}s")
            return None
        return text

    # Guards against two scans running at once -- both a cheap correctness
    # measure (ai_fallback's own dedicated bridge entity assumes exactly
    # one outstanding request at a time, see its own comment) and what
    # makes _run_scan_async's "already running? no-op" check meaningful.
    # _cancel_event is checked inside _on_scan_progress, which
    # scan_watch_dir/parse_lama_pdf both poll once per exercise — see
    # their own doc comments on why that's the real cancellation
    # checkpoint, not just a between-files one.
    _scan_lock = threading.Lock()
    _cancel_event = threading.Event()

    def _on_scan_progress(file_index: int, file_total: int, filename: str, exercise_done: int, exercise_total: int) -> bool:
        # Republished after every single exercise within the CURRENT file
        # (not just once per file) -- see scan_watch_dir's own doc comment
        # on why per-file granularity alone goes silent for the whole
        # duration of one large PDF. lama_catalog/lama_scan_status
        # arriving is what tells the UI the scan is over (see
        # lamaState.ts), so there's no separate "done" flag published
        # here.
        world.set_attribute(control, "lama_scan_progress", {
            "file_index": file_index, "file_total": file_total, "filename": filename,
            "exercise_done": exercise_done, "exercise_total": exercise_total,
        })
        return not _cancel_event.is_set()

    def publish_catalog() -> tuple[list[dict], list[dict]]:
        global questions_by_id
        _ensure_ai_test_running()
        questions_by_id, catalog, skipped = scan_watch_dir(ai_fallback=ai_fallback, on_progress=_on_scan_progress)
        world.set_many([
            (control, "lama_catalog", catalog),
            (control, "lama_skipped", skipped),
            (control, "lama_scan_status", {
                "pdf_count": len(list(WATCH_DIR.glob("*.pdf"))),
                "imported_count": len(catalog),
                "skipped_count": len(skipped),
                "scanned_at": time.time(),
                "cancelled": _cancel_event.is_set(),
            }),
            (control, "lama_recent_uploads", _recent_uploads()),
        ])
        verb = "cancelled mid-scan" if _cancel_event.is_set() else "scanned"
        print(f"[{name}] {verb} {WATCH_DIR}: {len(catalog)} importable, {len(skipped)} skipped", flush=True)
        return catalog, skipped

    def _run_scan_async(on_done: Optional[Callable[[list, list], None]] = None) -> bool:
        """Runs publish_catalog() on its own thread instead of the
        dispatch thread that on_lama_command itself runs on (see
        sotrice_client.py's own _dispatch_loop doc comment on why that's
        a single, strictly serial thread) -- a scan run synchronously
        there would make a live "cancel_scan" command queue up behind the
        very scan it's meant to interrupt, arriving only after it's
        already finished. Returns False (a no-op) if a scan is already
        running, rather than stacking a second one."""
        if not _scan_lock.acquire(blocking=False):
            return False
        _cancel_event.clear()

        def _worker():
            try:
                catalog, skipped = publish_catalog()
                if on_done is not None:
                    on_done(catalog, skipped)
            finally:
                _scan_lock.release()

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def _make_scan_result_reporter(safe_name: str) -> Callable[[list, list], None]:
        """Builds an on_done callback for _run_scan_async scoped to one
        specific filename -- shared by "upload_pdf" (brand new bytes just
        written) and "select_recent" (a file already sitting in
        WATCH_DIR), since both just want "what did THIS file produce"
        reported via lama_upload_result once the scan they kicked off
        finishes, not the whole watch folder's results."""
        def _report(catalog: list, skipped: list) -> None:
            imported = [c for c in catalog if c["source_pdf"] == safe_name]
            failed = [s for s in skipped if s["titel"] == safe_name]
            if not imported and not failed:
                # The file wrote (or already existed) and re-scanned fine,
                # but this exact PDF produced nothing at all either way --
                # still a real result worth reporting, not silence.
                world.set_attribute(control, "lama_upload_result", {
                    "filename": safe_name, "ok": False, "error": "parsed, but found no exercises in this PDF at all",
                })
            else:
                world.set_attribute(control, "lama_upload_result", {
                    "filename": safe_name,
                    "ok": True,
                    "imported_count": len(imported),
                    "skipped_count": len(failed),
                })
            print(f"[{name}] scanned {safe_name!r}: {len(imported)} importable, {len(failed)} skipped", flush=True)
        return _report

    publish_catalog()

    # Discovers every currently RUNNING math-teacher class the same way
    # math_student.py already does (see the module docstring's "Reads"
    # section) — {code: control_entity}, last write wins for a given
    # code.
    known_classes: dict[str, int] = {}

    def on_class_code(entity, attribute, value, source):
        if isinstance(value, str) and value:
            known_classes[value] = entity

    world.subscribe("class_code", on_class_code, replay=True)

    def on_lama_command(entity, attribute, value, source):
        if entity != control or not isinstance(value, dict):
            return
        cmd = value.get("cmd")

        if cmd == "upload_pdf":
            filename = value.get("filename")
            data_b64 = value.get("data_base64")
            safe_name = Path(filename).name if isinstance(filename, str) else ""
            if not safe_name.lower().endswith(".pdf") or not isinstance(data_b64, str) or not data_b64:
                world.set_attribute(control, "lama_upload_result", {
                    "filename": filename, "ok": False, "error": "not a valid PDF upload payload",
                })
                return
            try:
                raw = base64.b64decode(data_b64, validate=True)
            except Exception as exc:  # noqa: BLE001 - any bad base64 is a real, reportable upload failure, not a crash
                world.set_attribute(control, "lama_upload_result", {
                    "filename": safe_name, "ok": False, "error": f"couldn't decode the upload: {exc!r}",
                })
                return
            WATCH_DIR.mkdir(parents=True, exist_ok=True)
            (WATCH_DIR / safe_name).write_bytes(raw)

            if not _run_scan_async(on_done=_make_scan_result_reporter(safe_name)):
                world.set_attribute(control, "lama_upload_result", {
                    "filename": safe_name, "ok": False, "error": "a scan is already running -- try again once it finishes",
                })

        elif cmd == "select_recent":
            # Re-picks a PDF already sitting in WATCH_DIR (see
            # lama_recent_uploads) instead of a freshly-uploaded one --
            # see this module's own "select_recent" doc comment above.
            filename = value.get("filename")
            safe_name = Path(filename).name if isinstance(filename, str) else ""
            if not safe_name or not (WATCH_DIR / safe_name).is_file():
                world.set_attribute(control, "lama_upload_result", {
                    "filename": filename, "ok": False, "error": "that file isn't in the watch folder anymore -- try Upload PDF instead",
                })
                return
            if not _run_scan_async(on_done=_make_scan_result_reporter(safe_name)):
                world.set_attribute(control, "lama_upload_result", {
                    "filename": safe_name, "ok": False, "error": "a scan is already running -- try again once it finishes",
                })

        elif cmd == "rescan":
            _run_scan_async()

        elif cmd == "cancel_scan":
            _cancel_event.set()

        elif cmd == "import_into_class":
            class_code = value.get("class_code")
            question_ids = value.get("question_ids")
            if not isinstance(class_code, str) or class_code not in known_classes:
                return
            if not isinstance(question_ids, list) or not question_ids:
                return
            questions = [questions_by_id[qid] for qid in question_ids[:MAX_IMPORT_PER_REQUEST] if qid in questions_by_id]
            if not questions:
                return  # every id named was stale/unknown — nothing real to send

            payload: dict = {"cmd": "import_questions", "questions": questions}
            new_chapter_name = value.get("new_chapter_name")
            chapter_id = value.get("chapter_id")
            if isinstance(new_chapter_name, str) and new_chapter_name.strip():
                payload["new_chapter_name"] = new_chapter_name
            elif isinstance(chapter_id, str):
                payload["chapter_id"] = chapter_id

            target_entity = known_classes[class_code]
            world.set_attribute(target_entity, f"authoring_command:{class_code}", payload)
            print(f"[{name}] sent {len(questions)} question(s) to class {class_code!r}", flush=True)

        # Anything else (missing/unknown "cmd", extra junk keys) falls
        # through and is ignored, same posture as math_teacher.py's own
        # command handlers.

    world.subscribe("lama_command", on_lama_command, replay=False)

    print(f"[{name}] watching {WATCH_DIR} for LaMa PDFs (set LAMA_PDF_DIR to point elsewhere)", flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print(f"[{name}] stopped")
