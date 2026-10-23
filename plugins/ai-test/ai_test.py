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
       "request_id": "...optional, echoed back untouched...",
       "attachments": [{"mime_type": "audio/wav", "data_base64": "..."}]}
  `attachments` is optional and GENERIC — any calling spin may pass
  inline media (a recorded voice instruction, a screenshot, ...) that
  Gemini receives alongside the prompt as `inlineData` parts, in order.
  This file never inspects or interprets the bytes; what the media MEANS
  is the prompt's job, exactly like `context`. A request with no
  `attachments` key behaves byte-for-byte as it always has. See
  `normalize_attachments` for the validation limits.
  OPTIONAL tool calling (additive; requests without these keys behave
  exactly as before): `tools: [{name, description, parameters_schema}]`,
  `history` (caller-owned turns after the first prompt turn, incl. prior
  tool calls/results, see `normalize_history`), `system` (a system
  instruction). When Gemini elects to call a tool, `ai_response` carries
  `tool_call: {name, args, id, signature}`; the caller runs the tool and
  sends the next request with the call + its result appended to
  `history` (use a fresh request_id per step). `ai_response.usage` always
  reports {prompt, output, total} tokens.
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

MULTI-PROVIDER (P14): the provider adapters live in ai_providers.py
(Gemini, OpenAI, Anthropic Claude, local Ollama/OpenAI-compatible). The
user picks the provider in Settings (`ai_provider`); callers still just
send `ai_request` and read `ai_response` -- same shapes as above, plus
`ai_response.provider` and `ai_response.cost_usd` (an estimate, or None).
A request the chosen provider can't serve (e.g. audio on Claude) gets a
clear `error` naming a provider that can, never a silent drop. Control
attributes on the settings entity (owner-only): `ai_provider`,
`ai_set_key` {provider, key}, `ai_models` {provider: {budget, capable}},
`ai_base_url` {provider: url}, `ai_provider_test` {provider} -> result in
`ai_provider_test_result`, `ai_usage_reset`; published state:
`ai_providers` (capabilities/models table), `ai_provider_status`
{provider: configured}, `ai_usage` (per provider/model tokens + cost
estimate, persisted). Keys: one file per provider under SOTRICE_AI_DIR,
never logged. Where the text above says "Gemini", read "the chosen provider".

Registered in plugins/registry.json as "ai-test".
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

import ai_providers as ap
from sotrice_client import World

GEMINI_MODEL_BUDGET = ap.PROVIDERS["gemini"]["models"]["budget"]
GEMINI_MODEL_CAPABLE = ap.PROVIDERS["gemini"]["models"]["capable"]
VALID_COST_BIASES = ("auto", "cost", "quality")

# clients/python/ai_test.py -> parents[2] is the repo root local-secrets/ lives in.
_LOCAL_SECRETS_KEY_PATH = Path(__file__).resolve().parents[2] / "local-secrets" / "gemini_api_key.txt"


def _resolve_model(task_hint: Any, cost_bias: str) -> str:
    """Gemini default-model policy (kept for existing callers/tests); the
    multi-provider version is ap.resolve_model."""
    return ap.resolve_model("gemini", task_hint, cost_bias)


def load_api_key() -> Optional[str]:
    """Dev-only local-secrets Gemini key (bare key or GEMINI_API_KEY=...
    line). None, never raises, when absent."""
    try:
        text = _LOCAL_SECRETS_KEY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("GEMINI_API_KEY="):
            return line[len("GEMINI_API_KEY=") :].strip() or None
    return text.strip() or None


def _ai_dir() -> Path:
    """Per-user store, outside any plugin install dir. SOTRICE_AI_DIR overrides (tests)."""
    override = os.environ.get("SOTRICE_AI_DIR")
    return Path(override) if override else Path(os.environ.get("APPDATA") or Path.home()) / "Sotrice" / "ai"


def _key_path(provider: str = "gemini") -> Path:
    # gemini keeps its original filename so existing installs keep their key
    return _ai_dir() / ("gemini_api_key.txt" if provider == "gemini" else f"{provider}_api_key.txt")


def _saved_key_path() -> Path:
    return _key_path("gemini")


def load_saved_key(provider: str = "gemini") -> Optional[str]:
    try:
        return _key_path(provider).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_key(key: str, provider: str = "gemini") -> bool:
    try:
        path = _key_path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="utf-8")
        return True
    except OSError as exc:
        print(f"[ai-test] could not persist the {provider} API key ({exc}) -- it will work until this process exits")
        return False


def _load_json(name: str, default):
    try:
        return json.loads((_ai_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save_json(name: str, value) -> None:
    try:
        path = _ai_dir() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    except OSError as exc:
        print(f"[ai-test] could not persist {name} ({exc})")


# Gemini-shaped wrappers (existing tests/callers); the neutral validators
# live in ai_providers.
def normalize_attachments(raw: Any) -> list[dict]:
    return [{"inlineData": {"mimeType": a["mime_type"], "data": a["data_base64"]}} for a in ap.validate_attachments(raw)]


def normalize_tools(raw: Any) -> Optional[list[dict]]:
    return ap.gemini_tools(ap.validate_tools(raw))


def normalize_history(raw: Any) -> list[dict]:
    return ap.gemini_history(ap.validate_history(raw))


class Settings:
    """This process's live view of the user's AI settings. A change applies
    to the very next request, no restart. Thread-safe."""

    def __init__(self):
        self.lock = threading.Lock()
        saved = _load_json("settings.json", {})
        self.provider = saved.get("provider") if saved.get("provider") in ap.PROVIDERS else ap.DEFAULT_PROVIDER
        self.cost_bias = saved.get("cost_bias") if saved.get("cost_bias") in VALID_COST_BIASES else "auto"
        self.models: dict = saved.get("models") or {}
        self.base_urls: dict = saved.get("base_urls") or {}
        self.prices: dict = saved.get("prices") or {}
        self.keys: dict = {p: load_saved_key(p) for p in ap.PROVIDERS if ap.PROVIDERS[p]["needs_key"]}
        if not self.keys.get("gemini"):
            self.keys["gemini"] = load_api_key()  # dev fallback only
        self.usage: dict = _load_json("usage.json", {})
        self.on_change = None  # set by main(): republishes state
        self.scrub = None  # set by main(): overwrites a consumed secret attribute in Core

    def _persist(self):
        _save_json(
            "settings.json",
            {"provider": self.provider, "cost_bias": self.cost_bias, "models": self.models, "base_urls": self.base_urls, "prices": self.prices},
        )
        if self.on_change:
            self.on_change()

    def set_key(self, provider: str, key: str):
        key = key.strip()
        if provider not in ap.PROVIDERS or not ap.PROVIDERS[provider]["needs_key"] or not key:
            return
        with self.lock:
            self.keys[provider] = key
        if key != load_saved_key(provider):
            save_key(key, provider)
        if self.on_change:
            self.on_change()

    def on_api_key(self, entity: int, attribute: str, value: Any, source: Optional[str]):
        if isinstance(value, str):  # legacy attribute: Gemini key
            self.set_key("gemini", value)
            if value.strip() and self.scrub:
                self.scrub(entity, attribute)

    def on_set_key(self, entity, attribute, value, source):
        if isinstance(value, dict) and isinstance(value.get("provider"), str) and isinstance(value.get("key"), str):
            self.set_key(value["provider"], value["key"])
            if self.scrub:
                self.scrub(entity, attribute)

    def on_cost_bias(self, entity, attribute, value, source):
        if value in VALID_COST_BIASES:
            with self.lock:
                changed = value != self.cost_bias
                self.cost_bias = value
            if changed:
                self._persist()

    def on_provider(self, entity, attribute, value, source):
        if value in ap.PROVIDERS:
            with self.lock:
                changed = value != self.provider
                self.provider = value
            if changed:
                self._persist()

    def on_models(self, entity, attribute, value, source):
        if isinstance(value, dict):
            clean = {
                p: {t: m.strip() for t, m in tiers.items() if t in ap.TIERS and isinstance(m, str) and m.strip()}
                for p, tiers in value.items()
                if p in ap.PROVIDERS and isinstance(tiers, dict)
            }
            with self.lock:
                changed = clean != self.models
                self.models = clean
            if changed:
                self._persist()

    def on_base_urls(self, entity, attribute, value, source):
        if isinstance(value, dict):
            clean = {p: u.strip() for p, u in value.items() if p in ap.PROVIDERS and isinstance(u, str) and u.strip().startswith(("http://", "https://"))}
            with self.lock:
                changed = clean != self.base_urls
                self.base_urls = clean
            if changed:
                self._persist()

    def snapshot(self, provider: Optional[str] = None) -> dict:
        with self.lock:
            p = provider if provider in ap.PROVIDERS else self.provider
            return {
                "provider": p,
                "api_key": self.keys.get(p),
                "cost_bias": self.cost_bias,
                "models": dict(self.models.get(p) or {}),
                "base_url": self.base_urls.get(p),
                "prices": dict(self.prices),
            }

    def configured(self) -> dict:
        with self.lock:
            return {p: (bool(self.keys.get(p)) if ap.PROVIDERS[p]["needs_key"] else True) for p in ap.PROVIDERS}

    def record_usage(self, provider: str, model: str, usage: dict, cost: Optional[float]):
        with self.lock:
            row = self.usage.setdefault(provider, {}).setdefault(model, {"requests": 0, "prompt": 0, "output": 0, "cost_usd": 0.0})
            row["requests"] += 1
            row["prompt"] += usage.get("prompt", 0)
            row["output"] += usage.get("output", 0)
            row["cost_usd"] = round(row["cost_usd"] + (cost or 0.0), 6)
            snapshot = json.loads(json.dumps(self.usage))
        _save_json("usage.json", snapshot)
        if self.on_change:
            self.on_change()

    def reset_usage(self, *_):
        with self.lock:
            self.usage = {}
        _save_json("usage.json", {})
        if self.on_change:
            self.on_change()


class ReplayGate:
    """An ai_request must be answered EXACTLY ONCE, even when ai-test restarts and Core replays every request that is
    still sitting on its entity (each one would be billed again). Core is the source of truth: the previous instance's
    ai_response is still on the same entity, so a replayed request whose request_id equals that entity's latest
    response's request_id was already answered. Deterministic, no timer: `listen` subscribes to the responses first
    and waits (World.barrier) until their replay has been fully dispatched, only then subscribes to the requests.
    Until `go_live`, a delivered request is a replay. One without a request_id cannot be matched to a response: it is
    answered unless a response already exists on its entity (then it is skipped to avoid billing twice); both cases
    are logged. After `go_live`, requests are live and always answered."""

    def __init__(self, start, log=print):
        self._start = start
        self._log = log
        self._lock = threading.Lock()
        self._live = False
        self._answered: dict = {}      # entity -> request_id of the latest ai_response in Core (None-id responses count as {})
        self._responded: set = set()   # entities that have any ai_response

    def on_response(self, entity: int, _attribute: str, value: Any, _source: Optional[str]):
        if isinstance(value, dict):
            with self._lock:
                self._responded.add(entity)
                if value.get("request_id") is not None:
                    self._answered[entity] = value["request_id"]

    def on_request(self, entity: int, attribute: str, value: Any, source: Optional[str]):
        with self._lock:
            live = self._live
            answered, responded = self._answered.get(entity), entity in self._responded
        if not live and isinstance(value, dict):
            rid = value.get("request_id")
            if rid is not None and answered == rid:
                self._log(f"[ai-test] skipped replayed ai_request {rid!r} on entity {entity}: already answered")
                return
            if rid is None:
                if responded:
                    self._log(f"[ai-test] skipped replayed ai_request without request_id on entity {entity}: the entity already has a response")
                    return
                self._log(f"[ai-test] answering replayed ai_request without request_id on entity {entity} (no response exists yet)")
        self._start(entity, attribute, value, source)

    def go_live(self):
        with self._lock:
            self._live = True

    def listen(self, world):
        """Subscribe in the only safe order (see the class doc)."""
        world.subscribe("ai_response", self.on_response, replay=True)
        world.barrier()
        world.subscribe("ai_request", self.on_request, replay=True)
        world.barrier()
        self.go_live()


def main():
    with World() as world:
        name = world.identify("ai-test")
        settings = Settings()

        control = world.create_entity()
        published: dict = {}  # attribute -> json of the last value we wrote (see publish_state)
        published_lock = threading.Lock()

        def publish_state():
            with settings.lock:
                usage = json.loads(json.dumps(settings.usage))
                provider, bias = settings.provider, settings.cost_bias
                models, base_urls = dict(settings.models), dict(settings.base_urls)
            configured = settings.configured()
            # Publish only what CHANGED since the last publish. The control attributes are both state we publish and
            # settings we read back (ai_provider ...), and a push cannot be told apart from a user's write (an
            # anonymous writer is attributed to the entity owner). Re-publishing unchanged values meant two publishes
            # in flight (a key save followed by a provider switch) echoed back as "user input" and ping-ponged the
            # provider forever (seen live: gemini <-> anthropic, hundreds of flips per minute).
            current = [
                ("ai_provider", provider),
                ("ai_provider_status", configured),
                ("ai_api_key_configured", configured.get(provider, False)),
                ("ai_cost_bias", bias),
                ("ai_models", models),
                ("ai_base_url", base_urls),
                ("ai_usage", usage),
            ]
            with published_lock:
                changed = [(control, name, value) for name, value in current if published.get(name) != json.dumps(value, sort_keys=True)]
                published.update({name: json.dumps(value, sort_keys=True) for _c, name, value in changed})
            if changed:
                world.set_many(changed)

        settings.on_change = publish_state
        # A key written through Core would otherwise sit in the attribute
        # store (readable by anything allowed to read it) long after it was
        # saved to disk; overwrite it as soon as it has been consumed.
        settings.scrub = lambda entity, attribute: world.set_attribute(entity, attribute, "")
        world.set_many([(control, "ai_settings_ready", True), (control, "ai_providers", ap.describe_providers())])
        publish_state()
        snap = settings.snapshot()
        ready = bool(snap["api_key"]) or not ap.PROVIDERS[snap["provider"]]["needs_key"]
        print(f"[{name}] provider {snap['provider']} ({'ready' if ready else 'NO KEY yet'}) — watching for ai_request")

        world.subscribe("ai_api_key", settings.on_api_key, replay=True)
        world.subscribe("ai_set_key", settings.on_set_key, replay=False)
        world.subscribe("ai_cost_bias", settings.on_cost_bias, replay=True)
        world.subscribe("ai_provider", settings.on_provider, replay=True)
        world.subscribe("ai_models", settings.on_models, replay=True)
        world.subscribe("ai_base_url", settings.on_base_urls, replay=True)
        world.subscribe("ai_usage_reset", settings.reset_usage, replay=False)

        def handle_test(entity, attribute, value, source):
            if isinstance(value, dict) and value.get("provider") in ap.PROVIDERS:
                threading.Thread(target=_test_provider, args=(world, entity, settings, value["provider"]), daemon=True).start()

        world.subscribe("ai_provider_test", handle_test, replay=False)

        # Guards against re-answering the same request twice (replay on
        # restart) -- cheap insurance against double billing.
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
            system = value.get("system") if isinstance(value.get("system"), str) else None
            try:
                attachments = ap.validate_attachments(value.get("attachments"))
                tools = ap.validate_tools(value.get("tools"))
                history = ap.validate_history(value.get("history"))
            except ValueError as exc:
                print(f"[{name}] rejecting ai_request on entity {entity}: {exc}")
                world.set_attribute(entity, "ai_response", {"request_id": request_id, "text": None, "model": None, "error": f"bad request: {exc}"})
                return

            if request_id is not None:
                dedup_key = (entity, request_id)
                with answered_lock:
                    if dedup_key in answered:
                        return
                    answered.add(dedup_key)

            # One worker thread per request: never block sotrice_client's
            # single dispatch thread on a slow network call.
            threading.Thread(
                target=_answer_request,
                args=(world, name, settings, entity, request_id, prompt, value.get("context"), value.get("task_hint"), attachments, tools, history, system),
                daemon=True,
            ).start()

        ReplayGate(handle_request).listen(world)

        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


def _test_provider(world: World, entity: int, settings: "Settings", provider: str):
    """'Save & test': a free key/connection check via the model list, then
    ONE tiny budget-model call. Result goes to ai_provider_test_result."""
    spec = ap.PROVIDERS[provider]
    with settings.lock:
        key = settings.keys.get(provider)
        base_url = settings.base_urls.get(provider)
        overrides = dict(settings.models.get(provider) or {})
        prices = dict(settings.prices)
    result: dict = {"provider": provider, "ok": False, "error": None, "models": [], "reply": None, "latency_ms": None}
    started = time.perf_counter()
    try:
        if spec["needs_key"] and not key:
            raise ValueError(f"No {spec['label']} API key saved yet.")
        try:
            result["models"] = ap.list_models(provider, key, base_url)
        except RuntimeError:
            # Hosted providers: a failed list IS the key check. A local
            # server without /models is fine.
            if spec["needs_key"]:
                raise
        model = ap.resolve_model(provider, "simple", "cost", overrides)
        reply = ap.generate(provider, key, model, "Reply with exactly one word: pong", base_url=base_url, max_tokens=64)
        result.update(ok=True, reply=(reply["text"] or "")[:80], model=model)
        settings.record_usage(provider, model, reply["usage"], ap.estimate_cost_usd(model, reply["usage"], prices))
    except (ValueError, RuntimeError) as exc:
        result["error"] = str(exc)
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    world.set_attribute(entity, "ai_provider_test_result", {**result, "nonce": time.time()})


def _is_audio(att: dict) -> bool:
    return str(att.get("mime_type", "")).startswith("audio/")


def _transcribe_if_needed(settings: "Settings", snap: dict, attachments: Optional[list[dict]], prompt: str):
    """The chosen provider can't take audio (Claude, OpenAI chat, Ollama) but the user spoke: transcribe the clip with
    another CONFIGURED provider that can (one cheap budget-model call), then continue with the transcript as text.
    Returns (attachments, prompt, transcript|None). With no audio, or a provider that takes audio natively, nothing
    changes; with no audio-capable provider configured the normal capability error ("switch to Gemini ...") surfaces."""
    audio = [a for a in (attachments or []) if _is_audio(a)]
    if not audio or ap.PROVIDERS[snap["provider"]]["capabilities"]["audio"]:
        return attachments, prompt, None
    configured = settings.configured()
    helper = next((p for p, spec in ap.PROVIDERS.items() if spec["capabilities"]["audio"] and configured.get(p)), None)
    if helper is None:
        return attachments, prompt, None
    hs = settings.snapshot(helper)
    model = ap.resolve_model(helper, "simple", "cost", hs["models"])
    heard = ap.generate(helper, hs["api_key"], model, "Transcribe this audio exactly as spoken, in the language spoken. Reply with only the transcript.", None, audio, None, None, None, hs["base_url"])
    settings.record_usage(helper, model, heard["usage"], ap.estimate_cost_usd(model, heard["usage"], hs["prices"]))
    text = (heard["text"] or "").strip()
    if not text:
        raise RuntimeError("I couldn't make out any speech in that recording.")
    rest = [a for a in (attachments or []) if not _is_audio(a)]
    return rest or None, f'{prompt}\n\nThe user said (transcribed from their voice): "{text}"', text


def _answer_request(
    world: World,
    name: str,
    settings: "Settings",
    entity: int,
    request_id: Optional[str],
    prompt: str,
    context: Optional[dict],
    task_hint: Any,
    attachments: Optional[list[dict]] = None,
    tools: Optional[list[dict]] = None,
    history: Optional[list[dict]] = None,
    system: Optional[str] = None,
):
    started = time.perf_counter()
    snap = settings.snapshot()
    provider = snap["provider"]
    model = ap.resolve_model(provider, task_hint, snap["cost_bias"], snap["models"])
    transcript = None

    try:
        attachments, prompt, transcript = _transcribe_if_needed(settings, snap, attachments, prompt)
        result = ap.generate(provider, snap["api_key"], model, prompt, context, attachments, tools, history, system, snap["base_url"])
    except (ValueError, RuntimeError) as exc:
        print(f"[{name}] {provider} call failed for entity {entity}: {exc}")
        world.set_attribute(
            entity, "ai_response", {"request_id": request_id, "text": None, "model": model, "provider": provider, "error": str(exc)}
        )
        return

    cost = ap.estimate_cost_usd(model, result["usage"], snap["prices"])
    settings.record_usage(provider, model, result["usage"], cost)
    print(
        f"[{name}] answered entity {entity}'s request in {time.perf_counter() - started:.2f}s "
        f"({len(result['text'] or '')} chars, tool_call={bool(result['tool_call'])}, {provider}/{model}, tokens {result['usage']['total']})"
    )
    response = {
        "request_id": request_id,
        "text": result["text"],
        "model": model,
        "provider": provider,
        "error": None,
        "usage": result["usage"],
        "cost_usd": cost,
    }
    if result["tool_call"]:
        response["tool_call"] = result["tool_call"]
    if transcript:
        response["transcript"] = transcript
    world.set_attribute(entity, "ai_response", response)


if __name__ == "__main__":
    main()
