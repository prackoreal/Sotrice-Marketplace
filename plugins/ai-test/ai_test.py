"""Universal AI spin — SKELETON: proves ONE domain-agnostic request/
response mechanism, not any calling-spin's UI. Per [[project_universal_ai_spin_design]]
(see auto-memory), this is meant to be usable by the math spin's future
AI tab AND, later, a city spin's own "AI agent analyzing" the city, from
the exact same engine — the domain-specific work (what to ask, what
context to feed in, how to render the answer) belongs entirely in the
CALLING spin, never in here. This file has no idea who is asking or why.

Mechanism (the same "watch a request-shaped attribute by NAME, write a
response-shaped attribute back onto the same entity" relay pattern
economics_test.py already uses for building_road_distance_m/etc, not a
new idea):

- Subscribes to `ai_request` (replay=True — a late-starting ai-test
  instance should still answer a request that was already sitting there,
  the same reasoning physics_test.py's active_viewpoint subscription
  already uses). Expected shape:
      {"prompt": "...", "context": {...arbitrary JSON, optional...},
       "request_id": "...optional, echoed back untouched..."}
- Calls the Gemini API for real (see `call_gemini`), on its OWN worker
  thread per request — NOT inline in the push-dispatch callback, since
  sotrice_client.py's dispatch thread is single and sequential (see its
  own module docstring): a slow network call sitting in that callback
  would delay every OTHER push this same connection is waiting to
  deliver, to any subscriber, for as long as the API takes to answer.
- Publishes `ai_response` back onto the SAME entity once the call
  returns (or fails):
      {"request_id": ..., "text": "..." or None, "model": "...",
       "error": None or "..."}

Known, deliberate simplifications:
- No streaming, no multi-turn conversation state, no tool/function
  calling, no context-length management — one prompt in, one answer
  out.
- Retries a transient Gemini failure (429/5xx, or a network-level error
  with no HTTP status at all) up to GEMINI_MAX_ATTEMPTS times with
  exponential backoff (see `_is_transient`/`call_gemini`) — real,
  observed behavior under back-to-back load, not a hypothetical. An
  ordinary 4xx (bad key, malformed request) is NOT retried, since attempt
  2 would fail identically to attempt 1.

Settings (API key + cost/quality bias): a real, user-facing Settings UI
now exists (ai-test-ui, registered via this plugin's own `ui` field,
same "own small settings panel" shape as rendering-test-ui's
CesiumTokenPanel.tsx) — but it talks to THIS process the same way every
other plugin's UI talks to its own backend: plain Core attributes on a
control Entity this file creates at startup (`ai_api_key`,
`ai_cost_bias`), not a bespoke Tauri-only storage path. That's a
deliberate architectural choice, not a shortcut: a Cesium ion token is
pure browser-side rendering config (CesiumJS never leaves the webview),
but THIS process is a plain Python Core client with no Tauri IPC access
at all — it needs the actual key value to make real HTTP calls, so the
value has to travel over the exact same Core connection this file
already has open, the same way every other cross-process value in this
project already travels. `ai_api_key`/`ai_cost_bias` are additionally
registered under this plugin's own `owner_only_prefixes` in
registry.json, so a remote (joined) connection can never read or write
them — only ever a local UI/plugin, same trust boundary math-teacher's
`authoring_` prefix already relies on.

Falls back to local-secrets/gemini_api_key.txt (gitignored dev
convenience, see .gitignore's own /local-secrets/ entry) ONLY when no
`ai_api_key` has ever been set via the real Settings UI — so a dev
checkout with no Settings interaction keeps working exactly as before,
while a real install always prefers whatever the user actually typed in.

Automatic model selection: a caller MAY include `"task_hint": "simple"
| "complex"` in its `ai_request` payload (optional — absent/unrecognized
defaults to "complex", the safe/higher-quality choice for a caller that
never said). `"simple"` picks GEMINI_MODEL_BUDGET (cheapest available —
a short classification/extraction task doesn't need frontier reasoning);
`"complex"` picks GEMINI_MODEL_CAPABLE (the most capable model at the
SAME price point as the next two models down, as of 2026-10-06's pricing
— see `_resolve_model`'s own comment for why that makes "complex" free
to default to). The user's own `ai_cost_bias` setting
("auto"/"cost"/"quality") overrides this per-request hint when set to
anything other than "auto" — "cost" always picks BUDGET regardless of
hint, "quality" always picks CAPABLE, "auto" (the default) defers to
each request's own hint.

Registered in plugins/registry.json as "ai-test".
"""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from sotrice_client import World

# Current-generation Gemini line only — the 2.5 series (Pro/Flash/
# Flash-Lite) retires 2026-10-16. Two tiers, verified against
# ai.google.dev's own models/pricing docs at build time (2026-10-06) —
# revisit if Google's lineup/pricing shifts before this gets hardened
# further; don't trust these names as gospel past that date.
#
# GEMINI_MODEL_BUDGET: gemini-3.1-flash-lite is the single cheapest
# current text model ($0.25/$1.50 per 1M input/output tokens) — still
# "frontier-class" per Google's own framing, not a toy, just the
# cheapest real option, right for a short classification/extraction task
# that doesn't need deep reasoning.
#
# GEMINI_MODEL_CAPABLE: gemini-3.6-flash/3.7-flash/3.8-flash all share
# the exact same price ($0.75/$3.75 through 2026-12-31) — gemini-3.8-flash
# is the most capable of that same-priced trio, so picking it over the
# other two costs nothing extra. This is why an unlabeled/default caller
# safely defaults to "complex" (see the module docstring): the "quality"
# tier isn't meaningfully more expensive than its own same-priced
# siblings, only genuinely pricier than the BUDGET tier.
GEMINI_MODEL_BUDGET = "gemini-3.1-flash-lite"
GEMINI_MODEL_CAPABLE = "gemini-3.8-flash"
GEMINI_ENDPOINT_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT_S = 30.0

VALID_COST_BIASES = ("auto", "cost", "quality")


def _resolve_model(task_hint: Any, cost_bias: str) -> str:
    """Picks which model answers one request — see the module docstring's
    "Automatic model selection" section for the actual policy this
    implements. Never raises: an unrecognized/missing task_hint is
    treated the same as the documented "complex" default, same posture
    as every other "don't know? pick the safe default" choice in this
    file."""
    if cost_bias == "cost":
        return GEMINI_MODEL_BUDGET
    if cost_bias == "quality":
        return GEMINI_MODEL_CAPABLE
    return GEMINI_MODEL_BUDGET if task_hint == "simple" else GEMINI_MODEL_CAPABLE

# Real, observed behavior (2026-09-27 live verification): Gemini genuinely
# returns transient 5xx under ordinary back-to-back load, not just under
# some contrived failure test — a single attempt surfaces that straight
# to the teacher as an error for what a moment later succeeds fine. Only
# retries a response actually worth retrying: a 429/5xx status, or a
# network-level failure (timeout, connection reset) that says nothing
# about whether the REQUEST itself was bad. An ordinary 4xx (bad key, bad
# request) fails on attempt 2 exactly the same way it failed on attempt
# 1, so retrying it would only add latency before showing the same error.
GEMINI_MAX_ATTEMPTS = 3
GEMINI_RETRY_BASE_DELAY_S = 1.0
_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _TRANSIENT_HTTP_STATUSES
    if isinstance(exc, urllib.error.URLError):
        # Anything else that never got a real HTTP status back at all —
        # a timeout, connection reset, DNS hiccup — is exactly as
        # possibly-transient as a 5xx, not a statement about the request.
        return True
    return False

# clients/python/ai_test.py -> parents[1] is "clients", parents[2] is the
# repo root local-secrets/ actually lives in.
_LOCAL_SECRETS_KEY_PATH = Path(__file__).resolve().parents[2] / "local-secrets" / "gemini_api_key.txt"


def load_api_key() -> Optional[str]:
    """Dev-only convention, see module docstring. Returns None (never
    raises) if the file doesn't exist yet — that's a normal, expected
    state before a key has been dropped in, not an error.

    Accepts either a bare key on its own (matching cesium_ion_token.txt's
    plain-value convention) or a `GEMINI_API_KEY=...` line among others
    (e.g. a Cloud project id noted alongside it) — reads whichever shape
    is actually there rather than assuming one over the other."""
    try:
        text = _LOCAL_SECRETS_KEY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("GEMINI_API_KEY="):
            return line[len("GEMINI_API_KEY=") :].strip() or None
    stripped = text.strip()
    return stripped or None


def call_gemini(api_key: str, model: str, prompt: str, context: Optional[dict]) -> str:
    """Makes one real, synchronous call to the Gemini API and returns the
    model's plain text reply. Raises on any failure (bad key, network
    error, empty response) — the caller decides how to turn that into an
    `ai_response.error` string; this function's only job is "ask Gemini,
    return text or raise."

    Deliberately stdlib-only (`urllib.request`, not `requests` or the
    `google-genai` SDK) — every other plugin in this codebase runs under
    a plain system `python` with no project-local venv/requirements.txt,
    so a new pip dependency here would be the first of its kind and
    silently break on any machine that hasn't manually installed it.
    """
    full_prompt = prompt
    if context:
        # The calling spin already decided what belongs in `context` —
        # this just makes sure Gemini actually sees it, not just the
        # bare prompt. Plain JSON, not prose, so structure survives.
        full_prompt = f"{prompt}\n\nContext:\n{json.dumps(context)}"

    body = json.dumps({"contents": [{"parts": [{"text": full_prompt}]}]}).encode("utf-8")
    endpoint = GEMINI_ENDPOINT_TEMPLATE.format(model=model)
    request = urllib.request.Request(
        f"{endpoint}?key={api_key}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    payload = None
    for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT_S) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            if attempt == GEMINI_MAX_ATTEMPTS or not _is_transient(exc):
                raise
            delay = GEMINI_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
            print(f"ai_test: Gemini call attempt {attempt}/{GEMINI_MAX_ATTEMPTS} hit {exc!r} — retrying in {delay:.0f}s")
            time.sleep(delay)

    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {payload}")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    if not text:
        raise RuntimeError(f"Gemini returned an empty response: {payload}")
    return text


class Settings:
    """This process's own live, mutable view of `ai_api_key`/
    `ai_cost_bias` — see the module docstring's "Settings" section.
    Starts from local-secrets as a fallback default, then tracks
    whatever the real Settings UI (or any other local writer) pushes,
    for as long as this process runs; a setting changed mid-run applies
    to the very next request, no restart needed."""

    def __init__(self, fallback_api_key: Optional[str]):
        self.lock = threading.Lock()
        self.api_key = fallback_api_key
        self.cost_bias = "auto"

    def on_api_key(self, entity: int, attribute: str, value: Any, source: Optional[str]):
        if isinstance(value, str) and value.strip():
            with self.lock:
                self.api_key = value.strip()

    def on_cost_bias(self, entity: int, attribute: str, value: Any, source: Optional[str]):
        if value in VALID_COST_BIASES:
            with self.lock:
                self.cost_bias = value

    def snapshot(self) -> tuple[Optional[str], str]:
        with self.lock:
            return self.api_key, self.cost_bias


def main():
    with World() as world:
        name = world.identify("ai-test")
        fallback_key = load_api_key()
        settings = Settings(fallback_key)

        # A real control Entity — see the module docstring's "Settings"
        # section for why this travels over Core attributes rather than
        # a Tauri-only path. `ai_settings_ready` is the bare, replay-able
        # marker ai-test-ui discovers this entity by (same "subscribe to
        # a known bare name with replay=True" idiom every other plugin's
        # own UI discovery already uses) — its value (whether a key is
        # currently configured, from WHICHEVER source) is itself useful
        # display info, never the secret value itself.
        control = world.create_entity()
        world.set_many([
            (control, "ai_settings_ready", True),
            (control, "ai_api_key_configured", bool(fallback_key)),
            (control, "ai_cost_bias", settings.cost_bias),
        ])
        if fallback_key:
            print(f"[{name}] Gemini API key loaded from local-secrets (fallback default) — ready for real requests")
        else:
            print(
                f"[{name}] no API key yet (neither ai_api_key nor {_LOCAL_SECRETS_KEY_PATH}) — "
                "ai_request pushes will get a real error response, not silently hang"
            )

        world.subscribe("ai_api_key", settings.on_api_key, replay=True)
        world.subscribe("ai_cost_bias", settings.on_cost_bias, replay=True)

        # Guards against re-answering the exact same request twice (e.g.
        # a replay on process restart re-delivering a request this
        # process, or an earlier instance, already answered) — cheap
        # insurance against double-billing the same prompt to Gemini.
        answered_lock = threading.Lock()
        answered: set[tuple[int, Optional[str]]] = set()

        def handle_request(entity: int, attribute: str, value: Any, source: Optional[str]):
            if not isinstance(value, dict):
                print(f"[{name}] ignoring non-object ai_request on entity {entity}: {value!r}")
                return
            prompt = value.get("prompt")
            if not prompt:
                print(f"[{name}] ignoring ai_request on entity {entity} with no prompt")
                return
            request_id = value.get("request_id")
            context = value.get("context")
            task_hint = value.get("task_hint")

            dedup_key = (entity, request_id)
            with answered_lock:
                if dedup_key in answered:
                    return
                answered.add(dedup_key)

            # One worker thread per request — see module docstring for
            # why this must NOT run inline on sotrice_client's shared
            # dispatch thread.
            threading.Thread(
                target=_answer_request,
                args=(world, name, settings, entity, request_id, prompt, context, task_hint),
                daemon=True,
            ).start()

        world.subscribe("ai_request", handle_request, replay=True)

        print(f"[{name}] watching for ai_request — Ctrl+C to stop")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


def _answer_request(
    world: World,
    name: str,
    settings: "Settings",
    entity: int,
    request_id: Optional[str],
    prompt: str,
    context: Optional[dict],
    task_hint: Any,
):
    started = time.perf_counter()
    api_key, cost_bias = settings.snapshot()
    model = _resolve_model(task_hint, cost_bias)
    if not api_key:
        world.set_attribute(
            entity,
            "ai_response",
            {"request_id": request_id, "text": None, "model": model, "error": "no Gemini API key configured"},
        )
        return

    try:
        text = call_gemini(api_key, model, prompt, context)
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        print(f"[{name}] Gemini call failed for entity {entity}: {exc!r}")
        world.set_attribute(
            entity,
            "ai_response",
            {"request_id": request_id, "text": None, "model": model, "error": str(exc)},
        )
        return

    elapsed = time.perf_counter() - started
    print(f"[{name}] answered entity {entity}'s request in {elapsed:.2f}s ({len(text)} chars, model {model})")
    world.set_attribute(
        entity,
        "ai_response",
        {"request_id": request_id, "text": text, "model": model, "error": None},
    )


if __name__ == "__main__":
    main()
