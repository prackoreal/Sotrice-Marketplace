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

AI answer keys: after parsing, every question whose key is missing or only
guessed (lama_parser.needs_ai_key -- in practice the "Mutiple Choice"
exercises whose checked boxes couldn't be read, and free-response answers
that were only guessed from the last line) gets a draft key from the AI over
the same ai_request/ai_response mechanism as above. Built to be cheap and
safe, because the Gemini key is rate-limited and a wrong key silently
mis-grades students:
  - Content first: the answer read from the PDF itself (color-coded
    solution, checked boxes) always wins and is never sent to the AI.
  - Batched (KEY_BATCH_SIZE exercises per request), at most KEY_CONCURRENCY
    in flight, each with a small grayscale crop of the exercise attached
    (ai_request.attachments, 1-2 Gemini tiles) next to the flattened text.
  - Budget model first (task_hint "simple"); only what it declined or got
    wrong is retried on the capable model.
  - Every reply is validated against the question's own options (indexes in
    range, never "everything", the count the question states) before it
    becomes the real key; an accepted AI key also sets question["ai_answer"]
    so math-teacher-ui shows "AI answer -- check it" with one-click accept.
  - Cached on disk by content hash (.lama_ai_keys.json in WATCH_DIR), so a
    rescan, a restart or the same exercise in another PDF never asks again.
    "Unsure"/"invalid" verdicts are cached too (retry_ai_keys re-asks them).
  - A 429 is the service saying "later", not an answer: the batch backs off
    (shared across workers) and, if it still can't get through, its
    questions stay PENDING -- never "unsure", never cached.
  - No key / offline / ai-test missing: nothing breaks, questions import
    exactly as before, and lama_ai_status carries a calm message.
Parse results are cached per PDF content hash too (.lama_parse_cache/), and
startup announces itself (lama_scan_status, scanning=true) BEFORE the first
scan, which runs on its own thread.

Entities published:
  A single control entity, kind="lama_bank":
    lama_catalog (list[{"id", "titel", "grundkompetenz", "klasse",
      "quelle", "aufgabenformat", "type", "review_reason", "answer_state"
      ("pdf" | "verified" | "ai" | "guess" | "missing" -- none of them an
      error), "source_pdf"}]) — METADATA ONLY, safe to browse/search/filter
      freely; deliberately excludes the actual question content
      (options/pairs/accepted_answers/points) so this stays a small,
      cheap-to-push list even with thousands of catalog entries. The
      full content for a chosen id is only ever read internally by this
      plugin itself, at the moment it's actually filed into a class
      (see "import_into_class" below) — never published wholesale.
    lama_ai_status ({"state": "idle"|"ok"|"partial"|"unavailable",
      "message": calm user-facing text, + counters wanted/keyed/verified/
      cached/unsure/failed/skipped/pending/requests}) -- the last AI answer-
      key pass.
    lama_skipped (list[{"titel", "grundkompetenz", "klasse", "quelle",
      "aufgabenformat", "reason", "source_pdf", "whole_pdf"}]) — every exercise lama_parser.py
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
      "retry_ai_keys" — forgets cached AI verdicts that weren't a usable key
        (unsure/invalid) and rescans, so those questions are asked again --
        e.g. after adding an API key. Good answers stay cached.
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
import concurrent.futures
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from lama_parser import (
    METADATA_FIELDS,
    NO_ENTRY_REASON,
    PARSER_CACHE_VERSION,
    AiFallback,
    KeyOutcome,
    ParsedQuestion,
    ParseResult,
    apply_key_outcome,
    build_key_prompt,
    initial_answer_state,
    key_cache_id,
    needs_ai_key,
    parse_key_reply,
    parse_lama_pdf,
    render_region_png,
)
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

# AI answer-key pass (see the module docstring's "AI answer keys" section).
# Gemini rate-limits hard under parallel load (429/503 observed live with 6
# concurrent requests) and ai-test already retries transient failures with
# backoff, so this stays small: a few exercises per request, two in flight.
KEY_BATCH_SIZE = 5
KEY_CONCURRENCY = 2
# One batch's whole round trip, INCLUDING ai-test's own up-to-3 retries
# with backoff on a transient 429/5xx -- generous, but bounded.
KEY_REQUEST_TIMEOUT_S = 150.0
# Consecutive failed batches (timeouts, transport errors, unusable replies)
# before the pass gives up for this scan instead of burning a timeout per
# remaining batch against a service that clearly isn't answering.
KEY_MAX_CONSECUTIVE_FAILURES = 2
# A 429 is the service saying "later", not a verdict on the question: wait
# (exponentially, and for EVERY worker -- see _cooldown) and retry the same
# batch; only after this many tries do its questions stay "pending".
KEY_RATE_LIMIT_RETRIES = 3
KEY_RATE_LIMIT_BASE_DELAY_S = 20.0
# How long a scan that NEEDS AI keys waits for ai-test to announce itself
# (ai_settings_ready) after being started -- a cold Python start.
AI_TEST_READY_TIMEOUT_S = 20.0
# Lives in WATCH_DIR so it follows LAMA_PDF_DIR (an isolated test points
# that at scratch and gets an isolated cache for free) and so a teacher's
# PDFs and their already-paid-for AI answers travel together. A dotfile,
# not a *.pdf, so neither the PDF glob nor the recent-uploads list sees it.
KEY_CACHE_PATH = WATCH_DIR / ".lama_ai_keys.json"
# When THIS process started. Core keeps a dead plugin's entities and their
# last attribute values forever, so after any restart a UI replaying
# lama_catalog etc. sees every past instance's leftovers too -- this is
# what lets it tell which entity is the live one (the newest). Published as
# lama_scan_status.instance_started_at.
INSTANCE_STARTED_AT = time.time()


def _load_key_cache() -> dict[str, dict]:
    try:
        raw = json.loads(KEY_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return entries if isinstance(entries, dict) else {}


def _save_key_cache(entries: dict[str, dict]) -> None:
    try:
        WATCH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = KEY_CACHE_PATH.with_name(KEY_CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, KEY_CACHE_PATH)
    except OSError as exc:  # a cache that can't be written costs money next scan, never correctness now
        print(f"[lama-bank] couldn't write the AI key cache: {exc!r}", flush=True)


# Parsing a real LaMa PDF is slow (pdfplumber over every page: ~30s for the
# 300-exercise Typ-1 export) and used to happen in full, for EVERY PDF in
# the folder, on every plugin start and again after every single upload.
# Keyed by the file's own content hash + lama_parser.PARSER_CACHE_VERSION,
# so a re-uploaded identical file, a restart, and "upload one more PDF"
# all skip straight past the PDFs already parsed.
PARSE_CACHE_DIR = WATCH_DIR / ".lama_parse_cache"


def _parse_cache_path(pdf_path: Path) -> Optional[Path]:
    try:
        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    except OSError:
        return None
    return PARSE_CACHE_DIR / f"{digest}-v{PARSER_CACHE_VERSION}.json"


def _load_cached_parse(cache_path: Optional[Path]) -> Optional[ParseResult]:
    if cache_path is None:
        return None
    try:
        return ParseResult.from_json(json.loads(cache_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _store_parse(cache_path: Optional[Path], result: ParseResult) -> None:
    if cache_path is None:
        return
    try:
        PARSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        tmp.write_text(json.dumps(result.to_json(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache_path)
    except OSError as exc:  # a missing cache entry only costs time next scan
        print(f"[lama-bank] couldn't write the parse cache: {exc!r}", flush=True)


@dataclass
class AiReply:
    """What came back from one ai-test round trip -- text on success,
    error text from ai_response.error (or a local timeout note) on
    failure. `model` is whichever Gemini model ai-test picked."""
    text: Optional[str]
    error: Optional[str]
    model: Optional[str] = None

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
) -> tuple[dict[str, dict], list[dict], list[dict], dict[str, ParsedQuestion]]:
    """Parses every *.pdf in WATCH_DIR. Returns (questions_by_id — the
    FULL question content, never published wholesale, see the module
    docstring — catalog_entries, skipped_entries, parsed_by_id — the
    ParsedQuestion behind every catalog id, which is what the AI keying
    pass reads its context from and the only place `answer_source` lives).
    Every catalog entry starts with the answer_state a question has BEFORE
    any AI keying (see lama_parser.initial_answer_state). `ai_fallback`, if
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
    parsed_by_id: dict[str, ParsedQuestion] = {}
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

        cache_path = _parse_cache_path(pdf_path)
        fallback_failed = False

        def _tracked_fallback(prompt: str, _real: Optional[AiFallback] = ai_fallback) -> Optional[str]:
            nonlocal fallback_failed
            text = _real(prompt) if _real is not None else None
            if text is None:
                fallback_failed = True
            return text

        try:
            result = _load_cached_parse(cache_path)
            if result is not None:
                _file_progress(len(result.imported) + len(result.skipped), len(result.imported) + len(result.skipped))
            else:
                result = parse_lama_pdf(
                    str(pdf_path), ai_fallback=_tracked_fallback if ai_fallback is not None else None, on_progress=_file_progress,
                )
                # Never cache a partial (cancelled) parse, nor one where an
                # AI-fallback call FAILED (an outage would otherwise freeze
                # "couldn't extract this exercise" in for good).
                if not cancelled and not fallback_failed:
                    _store_parse(cache_path, result)
        except Exception as exc:  # noqa: BLE001 - one bad/corrupt PDF must not take the whole scan down
            skipped.append({
                "titel": pdf_path.name, "grundkompetenz": "", "klasse": "", "quelle": "",
                "aufgabenformat": "", "reason": f"failed to parse this PDF at all: {exc!r}",
                "source_pdf": pdf_path.name, "whole_pdf": True,
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
            parsed_by_id[catalog_id] = parsed
            catalog.append({
                "id": catalog_id,
                "titel": parsed.source.get("Titel", ""),
                "grundkompetenz": parsed.source.get("Grundkompetenz(en)", ""),
                "klasse": parsed.source.get("Klasse", ""),
                "quelle": parsed.source.get("Quelle", ""),
                "aufgabenformat": parsed.source.get("Aufgabenformat", ""),
                "type": parsed.question["type"],
                "review_reason": parsed.review_reason,
                "answer_state": initial_answer_state(parsed),
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
                "source_pdf": pdf_path.name, "whole_pdf": False,
            })

        if cancelled:
            break

    return questions_by_id, catalog, skipped, parsed_by_id


with World() as world:
    name = world.identify("lama-bank")

    control = world.create_entity()
    world.set_attribute(control, "kind", "lama_bank")

    questions_by_id: dict[str, dict] = {}

    # The ai_request/ai_response round trip to ai-test (see ai_test.py and
    # plugins/registry.json's "ai-test" entry) -- a second, later caller of
    # the exact same domain-agnostic mechanism math-teacher-ui's AI tab
    # and city_buildings.py already use; this plugin has no idea HOW
    # ai-test answers, only that it does. Two callers live here:
    #   - ai_fallback(): lama_parser's "geometry failed, ask the AI to
    #     extract this exercise" -- serial, one exercise at a time.
    #   - the answer-key pass (see key_pass below) -- a few batches in
    #     flight at once.
    # Concurrency means one shared request entity won't do: two near-
    # simultaneous writes of the same attribute on the same entity can
    # coalesce on the wire, so each in-flight request leases its OWN
    # entity from a small pool and matches its reply by a fresh
    # request_id.
    _AI_BRIDGE_POOL_SIZE = KEY_CONCURRENCY + 1
    _ai_bridge_pool: "queue.Queue[int]" = queue.Queue()
    for _ in range(_AI_BRIDGE_POOL_SIZE):
        _ai_bridge_pool.put(world.create_entity())
    _ai_pending: dict[str, threading.Event] = {}
    _ai_replies: dict[str, AiReply] = {}
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
            text, error, model = value.get("text"), value.get("error"), value.get("model")
            _ai_replies[request_id] = AiReply(
                text=text if isinstance(text, str) else None,
                error=error if isinstance(error, str) else None,
                model=model if isinstance(model, str) else None,
            )
        event.set()

    world.subscribe("ai_response", _on_ai_response, replay=False)

    # ai-test announces itself with a bare ai_settings_ready the moment its
    # own startup is done (see ai_test.py's main()) -- replay=True so this
    # also fires for an instance that was already running.
    _ai_test_ready = threading.Event()
    world.subscribe("ai_settings_ready", lambda entity, attribute, value, source: _ai_test_ready.set(), replay=True)

    def _ensure_ai_test_running() -> bool:
        """True if an ai-test instance is running (or was just started).
        False only if it can't be (not installed, no way to ask) -- the
        caller then treats AI as unavailable instead of waiting out a
        request timeout against a process that will never answer."""
        try:
            running = world.list_running()
        except Exception as exc:  # noqa: BLE001 - a query failure must not block a scan over a nice-to-have
            print(f"[{name}] couldn't check for a running ai-test instance: {exc!r}")
            return False
        if any(r.get("plugin_type") == "ai-test" for r in running):
            return True
        try:
            world.start_plugin("ai-test")
        except Exception as exc:  # noqa: BLE001 - AI is optional; its absence just means fewer recovered exercises/keys
            print(f"[{name}] couldn't start ai-test for AI-assisted parsing: {exc!r}")
            return False
        return True

    def ai_request(prompt: str, timeout_s: float, extra: Optional[dict[str, Any]] = None) -> AiReply:
        entity = _ai_bridge_pool.get()
        request_id = str(uuid.uuid4())
        event = threading.Event()
        try:
            with _ai_lock:
                _ai_pending[request_id] = event
            world.set_attribute(entity, "ai_request", {"prompt": prompt, "request_id": request_id, **(extra or {})})
            got_reply = event.wait(timeout_s)
            with _ai_lock:
                _ai_pending.pop(request_id, None)
                reply = _ai_replies.pop(request_id, None)
        finally:
            _ai_bridge_pool.put(entity)
        if not got_reply or reply is None:
            return AiReply(text=None, error=f"no answer from the AI within {timeout_s:.0f}s")
        return reply

    def ai_fallback(prompt: str) -> Optional[str]:
        # Shares the key pass's rate-limit cooldown: while Gemini is saying
        # "slow down", asking again per failed exercise (each ai-test call
        # already retries 3x internally) only digs the hole deeper.
        if time.time() < _cooldown["until"]:
            return None
        reply = ai_request(prompt, _AI_FALLBACK_TIMEOUT_S)
        if reply.error:
            print(f"[{name}] AI fallback failed: {reply.error}")
            if _is_rate_limited(reply.error):
                _cooldown["until"] = time.time() + KEY_RATE_LIMIT_BASE_DELAY_S
        return reply.text

    # ---- AI answer keys -------------------------------------------------
    # See the module docstring's "AI answer keys" section for the why;
    # this is the how. Runs AFTER parsing, over every question whose key
    # is missing or only guessed (lama_parser.needs_ai_key), and mutates
    # those questions + their catalog entries in place: a validated AI
    # answer becomes the question's real key plus an ai_answer marker,
    # anything else leaves the question exactly as parsing produced it.

    # Failure reasons that mean "nothing further in this pass can succeed"
    # -- stop at once instead of burning a request per remaining batch.
    _FATAL_AI_ERRORS = (
        ("no gemini api key", "no_key"),
        ("api key not valid", "bad_key"),
        ("api_key_invalid", "bad_key"),
        ("permission_denied", "bad_key"),
    )

    def _classify_ai_error(error: str) -> str:
        low = error.lower()
        for needle, reason in _FATAL_AI_ERRORS:
            if needle in low:
                return reason
        return "unreachable" if "no answer from the ai" in low or "urlopen" in low or "timed out" in low else "error"

    def _ai_status(state: str, message: str, stats: dict[str, int]) -> dict[str, Any]:
        return {"state": state, "message": message, **stats}

    def _status_for(stats: dict[str, int], fatal: Optional[str], cancelled: bool) -> dict[str, Any]:
        wanted, resolved = stats["wanted"], stats["keyed"] + stats["verified"]
        waiting = stats["unsure"] + stats["failed"] + stats["skipped"] + stats["pending"]
        if wanted == 0:
            return _ai_status("idle", "", stats)
        if fatal == "no_key":
            return _ai_status(
                "unavailable",
                f"{waiting} question{'s' if waiting != 1 else ''} can't get an AI answer key yet: no Gemini API key is set up. "
                "Add one in the AI settings, then rescan -- until then they import without an answer key (you can still set it by hand).",
                stats,
            )
        if fatal == "bad_key":
            return _ai_status("unavailable", "The Gemini API key was rejected, so AI answer keys are off. Check the key in the AI settings, then rescan.", stats)
        if fatal in ("unreachable", "error"):
            return _ai_status(
                "unavailable",
                f"Couldn't reach the AI service just now, so {waiting} question{'s' if waiting != 1 else ''} are waiting for an answer key. Rescan to try again.",
                stats,
            )
        if fatal == "rate_limited" or stats["pending"]:
            return _ai_status(
                "partial",
                f"The AI service is busy right now (rate limit): AI drafted keys for {resolved} of {wanted} questions and "
                f"{stats['pending']} are still pending. Nothing is lost -- they're asked again on the next rescan.",
                stats,
            )
        if cancelled:
            return _ai_status("partial", f"Stopped early: AI drafted keys for {resolved} of {wanted}; the rest are waiting for an answer key.", stats)
        if stats["failed"] or stats["unsure"] or stats["skipped"] or stats["pending"]:
            return _ai_status(
                "partial",
                f"AI drafted answer keys for {resolved} of {wanted} questions. The other {waiting} weren't clear enough to answer "
                "(often they need a diagram) and are waiting for an answer key.",
                stats,
            )
        return _ai_status("ok", f"AI drafted answer keys for {resolved} question{'s' if resolved != 1 else ''}. Look them over before class -- they're marked \"AI answer\".", stats)

    def _publish_keying_progress(done: int, total: int) -> None:
        world.set_attribute(control, "lama_scan_progress", {
            "phase": "keying", "file_index": 1, "file_total": 1, "filename": "",
            "exercise_done": done, "exercise_total": total,
        })

    def _is_rate_limited(error: Optional[str]) -> bool:
        low = (error or "").lower()
        return "429" in low or "too many requests" in low or "resource_exhausted" in low or "rate limit" in low

    # Shared "Gemini told us to slow down" deadline: when ONE batch hits a
    # 429, every worker waits it out instead of hammering the same wall.
    _cooldown = {"until": 0.0}
    _render_lock = threading.Lock()  # pdfplumber documents aren't thread-safe

    def key_pass(parsed_by_id: dict[str, ParsedQuestion], catalog: list[dict]) -> dict[str, Any]:
        catalog_by_id = {c["id"]: c for c in catalog}
        wanted = [(cid, pq) for cid, pq in parsed_by_id.items() if needs_ai_key(pq)]
        stats = {"wanted": len(wanted), "keyed": 0, "verified": 0, "cached": 0, "unsure": 0, "failed": 0, "skipped": 0, "pending": 0, "requests": 0}
        if not wanted:
            return _status_for(stats, None, False)

        cache = _load_key_cache()

        def settle(cid: str, pq: ParsedQuestion, status: str, outcome, model: Optional[str]) -> None:
            state = apply_key_outcome(pq, outcome, model)
            catalog_by_id[cid]["answer_state"] = state
            if status == "ok":
                stats["verified" if state == "verified" else "keyed"] += 1
            elif status == "unsure":
                stats["unsure"] += 1
            else:
                stats["failed"] += 1

        # Identical exercises (same content hash -- the same question in
        # two PDFs, or repeated within one) are asked ONCE and the answer
        # applied to every copy.
        groups: dict[str, list[tuple[str, ParsedQuestion]]] = {}
        for cid, pq in wanted:
            cache_id = key_cache_id(pq)
            hit = cache.get(cache_id)
            if isinstance(hit, dict) and hit.get("status") in ("ok", "unsure", "invalid"):
                outcome = KeyOutcome(
                    hit["status"], correct_indices=hit.get("correct_indices"), answer=hit.get("answer"),
                    rationale=hit.get("rationale") or "", reason=hit.get("reason") or "",
                )
                stats["cached"] += 1
                settle(cid, pq, hit["status"], outcome, hit.get("model"))
            else:
                groups.setdefault(cache_id, []).append((cid, pq))
        if not groups:
            return _status_for(stats, None, _cancel_event.is_set())

        fatal: Optional[str] = None
        if not _ensure_ai_test_running() or not _ai_test_ready.wait(AI_TEST_READY_TIMEOUT_S):
            stats["skipped"] = sum(len(g) for g in groups.values())
            print(f"[{name}] ai-test isn't available -- {stats['skipped']} question(s) stay without an AI key", flush=True)
            return _status_for(stats, "unreachable", False)

        open_pdfs: dict[str, Any] = {}

        def image_for(cid: str, pq: ParsedQuestion) -> Optional[str]:
            region = (pq.ai_context or {}).get("region")
            if not region:
                return None
            pdf_name = cid.rsplit(":", 1)[0]
            with _render_lock:
                if pdf_name not in open_pdfs:
                    try:
                        import pdfplumber
                        open_pdfs[pdf_name] = pdfplumber.open(WATCH_DIR / pdf_name)
                    except Exception:  # noqa: BLE001 - no picture, text-only
                        open_pdfs[pdf_name] = None
                pdf = open_pdfs[pdf_name]
                png = render_region_png(pdf, region) if pdf is not None else None
            return base64.b64encode(png).decode("ascii") if png else None

        accounted: set[str] = set()  # cache ids already counted as pending/failed
        recorded: set[str] = set()  # cache ids whose verdict is already applied + saved
        gave_up = threading.Event()  # set once ANY batch exhausts its rate-limit patience: the rest stop at once

        def run_batch(batch, task_hint: str):
            """batch: [(cache_id, [(cid, pq), ...])]. Always returns (batch,
            outcomes | None, model, error | None, rate_limited) -- never
            raises. A 429 is waited out and retried here (a rate limit says
            nothing about the QUESTION, so it must never become "unsure")."""
            try:
                items, attachments = [], []
                for cache_id, members in batch:
                    cid, pq = members[0]
                    item = {"id": cache_id, "type": pq.question["type"], "text": pq.question["text"],
                            "options": pq.question.get("options"), "context": pq.ai_context}
                    b64 = image_for(cid, pq)
                    if b64:
                        attachments.append({"mime_type": "image/png", "data_base64": b64})
                        item["image_index"] = len(attachments)
                    items.append(item)
                prompt = build_key_prompt(items)
                reply = None
                for attempt in range(KEY_RATE_LIMIT_RETRIES + 1):
                    if gave_up.is_set():
                        return batch, None, None, "rate limited", True
                    wait = _cooldown["until"] - time.time()
                    if wait > 0 and _cancel_event.wait(wait):
                        return batch, None, None, "cancelled", False
                    payload_extra = {"task_hint": task_hint}
                    if attachments:
                        payload_extra["attachments"] = attachments
                    stats["requests"] += 1
                    reply = ai_request(prompt, KEY_REQUEST_TIMEOUT_S, extra=payload_extra)
                    if not _is_rate_limited(reply.error):
                        break
                    delay = KEY_RATE_LIMIT_BASE_DELAY_S * (2 ** attempt)
                    _cooldown["until"] = max(_cooldown["until"], time.time() + delay)
                    print(f"[{name}] AI rate-limited (attempt {attempt + 1}); backing off {delay:.0f}s", flush=True)
                if reply is None or _is_rate_limited(reply.error):
                    gave_up.set()
                    return batch, None, None, reply.error if reply else "rate limited", True
                if reply.error or reply.text is None:
                    return batch, None, reply.model, reply.error or "empty reply", False
                return batch, parse_key_reply(reply.text, items), reply.model, None, False
            except Exception as exc:  # noqa: BLE001
                return batch, None, None, repr(exc), False

        def run_round(units, task_hint: str):
            """Asks every unit once at `task_hint`'s model tier. Returns
            ({cache_id: (outcome, model)}, units_left_pending)."""
            nonlocal fatal
            results: dict[str, tuple[KeyOutcome, Optional[str]]] = {}
            batches = [units[i:i + KEY_BATCH_SIZE] for i in range(0, len(units), KEY_BATCH_SIZE)]
            failures = 0
            left: list = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=KEY_CONCURRENCY) as pool:
                pending = {pool.submit(run_batch, b, task_hint) for b in batches}
                while pending:
                    finished, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                    for fut in finished:
                        if fut.cancelled():
                            continue
                        batch, outcomes, model, error, rate_limited = fut.result()
                        if rate_limited:
                            # Out of patience for NOW: leave these pending
                            # (never cached, never "unsure") and stop asking.
                            left.extend(batch)
                            accounted.update(cid_ for cid_, _m in batch)
                            stats["pending"] += sum(len(m) for _, m in batch)
                            fatal = fatal or "rate_limited"
                        elif outcomes is None or all(o.reason == NO_ENTRY_REASON for o in outcomes.values()):
                            failures += 1
                            left.extend(batch)
                            reason = _classify_ai_error(error) if error else "error"
                            print(f"[{name}] AI key batch failed ({reason}): {error or 'unusable reply'}", flush=True)
                            if reason in ("no_key", "bad_key"):
                                fatal = reason
                            elif failures >= KEY_MAX_CONSECUTIVE_FAILURES:
                                fatal = reason
                            accounted.update(cid_ for cid_, _m in batch)
                            stats["failed"] += sum(len(m) for _, m in batch)
                        else:
                            failures = 0
                            for cache_id, _members in batch:
                                results[cache_id] = (outcomes[cache_id], model)
                                if outcomes[cache_id].status == "ok":
                                    # A usable key is paid for -- apply and save it NOW, not at
                                    # the end of the pass, so a cancel/crash/rate limit later
                                    # never throws away answers that already cost tokens.
                                    record(cache_id, outcomes[cache_id], model)
                            _save_key_cache(cache)
                        _publish_keying_progress(len(results), len(units))
                    if fatal is not None or _cancel_event.is_set():
                        for fut in pending:
                            fut.cancel()
                        break
            return results, left

        def record(cache_id: str, outcome: KeyOutcome, model: Optional[str]) -> None:
            if cache_id in recorded:
                return
            recorded.add(cache_id)
            cache[cache_id] = {
                "status": outcome.status, "correct_indices": outcome.correct_indices, "answer": outcome.answer,
                "rationale": outcome.rationale, "reason": outcome.reason, "model": model, "at": time.time(),
            }
            for cid, pq in groups[cache_id]:
                settle(cid, pq, outcome.status, outcome, model)

        units = list(groups.items())
        _publish_keying_progress(0, len(units))
        # Round 1: the budget model. Round 2: ONLY what round 1 declined or
        # got wrong gets the capable (pricier) model -- most exercises never
        # reach it.
        first, left = run_round(units, "simple")
        undecided = [(cid_, groups[cid_]) for cid_, (o, _m) in first.items() if o.status != "ok"]
        decided = {cid_: v for cid_, v in first.items() if v[0].status == "ok"}
        if undecided and fatal is None and not _cancel_event.is_set():
            second, left2 = run_round(undecided, "complex")
            decided.update(second)
            for cid_, (o, m) in first.items():
                if cid_ not in second and cid_ not in decided:
                    decided[cid_] = (o, m)  # escalation never answered -- keep round 1's verdict
            left = left + left2
        else:
            for cid_, v in first.items():
                decided.setdefault(cid_, v)
        for cache_id, (outcome, model) in decided.items():
            record(cache_id, outcome, model)
        _save_key_cache(cache)

        for pdf in open_pdfs.values():
            try:
                if pdf is not None:
                    pdf.close()
            except Exception:  # noqa: BLE001
                pass

        settled_ids = set(decided)
        # Anything neither answered nor failed outright is simply not
        # decided yet: pending after a rate limit, "not attempted" if the
        # pass stopped early. No verdict, so no cache entry -- a rescan asks.
        for cache_id, members in groups.items():
            if cache_id not in settled_ids and cache_id not in accounted:
                stats["skipped"] += len(members)
        print(
            f"[{name}] AI answer keys: {stats['keyed']} drafted, {stats['verified']} verified, {stats['cached']} from cache, "
            f"{stats['unsure']} unsure, {stats['failed']} failed, {stats['pending']} pending (rate limit), "
            f"{stats['skipped']} not attempted, {stats['requests']} request(s)",
            flush=True,
        )
        return _status_for(stats, fatal, _cancel_event.is_set())

    # Guards against two scans running at once -- what makes
    # _run_scan_async's "already running? no-op" check meaningful.
    # _cancel_event is checked inside _on_scan_progress, which
    # scan_watch_dir/parse_lama_pdf both poll once per exercise — see
    # their own doc comments on why that's the real cancellation
    # checkpoint, not just a between-files one — and between key batches.
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
            "phase": "parsing",
            "file_index": file_index, "file_total": file_total, "filename": filename,
            "exercise_done": exercise_done, "exercise_total": exercise_total,
        })
        return not _cancel_event.is_set()

    def publish_catalog() -> tuple[list[dict], list[dict]]:
        global questions_by_id
        _ensure_ai_test_running()
        scanned_questions, catalog, skipped, parsed_by_id = scan_watch_dir(ai_fallback=ai_fallback, on_progress=_on_scan_progress)
        ai_status = key_pass(parsed_by_id, catalog)
        # Swapped in only now, once keying has finished mutating these
        # question dicts, so an import_into_class that lands mid-scan
        # still reads the previous, consistent snapshot (the one the UI
        # is still showing) instead of half-keyed content.
        questions_by_id = scanned_questions
        world.set_many([
            (control, "lama_catalog", catalog),
            (control, "lama_skipped", skipped),
            (control, "lama_ai_status", ai_status),
            (control, "lama_scan_status", {
                "pdf_count": len(list(WATCH_DIR.glob("*.pdf"))),
                "imported_count": len(catalog),
                "skipped_count": len(skipped),
                "scanned_at": time.time(),
                "cancelled": _cancel_event.is_set(),
                "scanning": False,
                "instance_started_at": INSTANCE_STARTED_AT,
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
            skipped_here = [x for x in skipped if x.get("source_pdf") == safe_name]
            whole_pdf_failures = [x for x in skipped_here if x.get("whole_pdf")]
            if whole_pdf_failures:
                # The PDF itself couldn't be read at all -- a real failure,
                # not "0 exercises parsed, 1 skipped".
                world.set_attribute(control, "lama_upload_result", {
                    "filename": safe_name, "ok": False, "error": whole_pdf_failures[0]["reason"],
                })
            elif not imported:
                # The file wrote (or already existed) and re-scanned fine,
                # but this exact PDF produced no importable exercise --
                # still a real result worth reporting, not silence.
                detail = f" ({len(skipped_here)} skipped: {skipped_here[0]['reason']})" if skipped_here else ""
                world.set_attribute(control, "lama_upload_result", {
                    "filename": safe_name, "ok": False, "error": "parsed, but found no importable exercises in this PDF" + detail,
                })
            else:
                world.set_attribute(control, "lama_upload_result", {
                    "filename": safe_name,
                    "ok": True,
                    "imported_count": len(imported),
                    "skipped_count": len(skipped_here),
                })
            print(f"[{name}] scanned {safe_name!r}: {len(imported)} importable, {len(skipped_here)} skipped", flush=True)
        return _report

    # Announce BEFORE scanning anything: the UI's start-up wait (lamaState.ts)
    # gives up after a few seconds if lama_scan_status hasn't arrived, and a
    # real watch folder (a 300-exercise Typ-1 export alone is ~30s to parse
    # cold) used to keep this plugin silent -- and deaf to lama_command --
    # for that whole time, which the UI reported as "LaMa plugin didn't
    # start in time". The first scan now runs on its own thread, right
    # after the command subscription below is live.
    world.set_many([
        (control, "lama_catalog", []),
        (control, "lama_skipped", []),
        (control, "lama_ai_status", _status_for({"wanted": 0, "keyed": 0, "verified": 0, "cached": 0, "unsure": 0, "failed": 0, "skipped": 0, "pending": 0, "requests": 0}, None, False)),
        (control, "lama_scan_status", {
            "pdf_count": len(list(WATCH_DIR.glob("*.pdf"))),
            "imported_count": 0, "skipped_count": 0, "scanned_at": None, "cancelled": False, "scanning": True,
            "instance_started_at": INSTANCE_STARTED_AT,
        }),
        (control, "lama_recent_uploads", _recent_uploads()),
    ])

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

        elif cmd == "retry_ai_keys":
            # Forgets every cached AI verdict that ISN'T a usable key
            # ("unsure"/"invalid" -- a declined or failed answer is cached
            # on purpose, so a plain rescan doesn't re-pay for it) and
            # rescans, so those exercises get asked again -- e.g. after
            # the teacher added an API key. Good answers stay cached.
            if _scan_lock.locked():
                return
            cache = _load_key_cache()
            kept = {k: v for k, v in cache.items() if isinstance(v, dict) and v.get("status") == "ok"}
            if len(kept) != len(cache):
                _save_key_cache(kept)
            _run_scan_async()

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
    _run_scan_async()  # the first scan -- see the startup announcement above

    print(f"[{name}] watching {WATCH_DIR} for LaMa PDFs (set LAMA_PDF_DIR to point elsewhere)", flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print(f"[{name}] stopped")
