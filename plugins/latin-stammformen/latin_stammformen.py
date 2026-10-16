"""Stammformen Trainer (latin-stammformen) -- Latin principal-parts trainer.

A Python plugin plus a spin-ui iframe UI (latin-stammformen-ui/), shipped as
one .spin. The spin ships EMPTY: the word list is imported by the user (a
JSON backup of this spin, or the original single-file Gemini page), so no
textbook content is ever part of the package.

Where things live:
  latin_logic.py    pure rules: verb validation, import parsers, Test-mode
                    answer checking, scan-reply parsing.
  latin_store.py    the user's data in the spin's OWN data directory
                    (%APPDATA%\\Sotrice\\latin-stammformen\\, or
                    SOTRICE_LATIN_DIR) -- outside the folder an install or
                    update replaces, so updates never touch it.
  latin_service.py  every command the UI can send; no Core in there.
  this file         the Core wiring: one control entity, a command/reply
                    protocol over plain attributes, and the bridge to the
                    ai-test spin for photo scans.

Core protocol (all on this plugin's own control entity):
  latin_ready  {version, data_dir, started_at}   announces the entity (UI
               discovers it by this bare name, replay-subscribed).
  latin_state  the full snapshot: verbs (each with its stored id), groups,
               exclusions, mastery by id, warnings. Republished after every
               change; `rev` only ever grows.
  latin_cmd    UI -> plugin: {sid, items: [{seq, cmd, ...}]}. The UI re-sends
               every still-unanswered item with each new command, and this
               side executes a (sid, seq) at most once, so a push that Core
               coalesces under load can never lose or double an edit.
  latin_reply  plugin -> UI: {n, items: [{sid, seq, ok, partial?, ...}]} --
               the last REPLY_RING replies (cumulative for the same reason).
               A `partial` item is progress (and acknowledges receipt); the
               final item resolves the request.

Photo scan: the image goes through the existing ai-test spin
(ai_request.attachments -- ai-test >= 0.1.2), so the user's own configured
Gemini key pays and this plugin never sees a key. See latin_service.py for
the budget-model-first / cache-by-hash pipeline.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

import latin_logic as L
from latin_service import AiReply, CommandError, MIN_AI_VERSION, Service
from latin_store import ReadOnlyError, ScanCache, Store, StoreError, data_dir

PLUGIN_TYPE = "latin-stammformen"
REPLY_RING = 24
HEAVY_COMMANDS = {"scan"}
SEEN_SESSIONS_MAX = 32


def registered_version(world: Any) -> Optional[str]:
    """This spin's own version as Sotrice registered it at install time (the
    installer doesn't unpack manifest.json next to the script, so the server's
    registry is the one authoritative source for an installed spin)."""
    try:
        for plugin in world.list_plugins():
            if plugin.get("type") == PLUGIN_TYPE and isinstance(plugin.get("version"), str):
                return plugin["version"]
    except Exception as exc:  # noqa: BLE001 - a version label must never stop the plugin
        print(f"[{PLUGIN_TYPE}] couldn't read the registered version: {exc!r}", flush=True)
    return None


def read_version() -> str:
    """Fallback for a dev checkout (not installed through the registry): the
    repo's committed manifest."""
    here = Path(__file__).resolve()
    for candidate in (here.with_name("manifest.json"), here.parents[2] / "plugins" / "spin-manifests" / f"{PLUGIN_TYPE}.json"):
        try:
            return str(json.loads(candidate.read_text(encoding="utf-8")).get("version", "dev"))
        except (OSError, ValueError):
            continue
    return "dev"


class CoreAiBridge:
    """Talks to ai-test over Core: writes `ai_request` on a private request
    entity and waits for the matching `ai_response` (matched by request_id).
    Pre-flight status comes from the plugin registry (is it installed, which
    version), the running list, and ai-test's own control-entity attributes
    (is a key configured, which cost bias)."""

    def __init__(self, world: Any, log_name: str) -> None:
        self.world = world
        self.log_name = log_name
        self.entity = world.create_entity()
        self.lock = threading.Lock()  # one AI request in flight at a time
        self.state_lock = threading.Lock()
        self.pending: dict[str, threading.Event] = {}
        self.replies: dict[str, AiReply] = {}
        # Core keeps a dead plugin's attributes forever, so these are keyed by
        # the SOURCE (instance name == launch id) and only the sources that
        # are running right now are consulted.
        self.key_configured: dict[str, bool] = {}
        self.cost_bias: dict[str, str] = {}
        world.subscribe("ai_response", self._on_response, replay=False)
        world.subscribe("ai_api_key_configured", self._on_key, replay=True)
        world.subscribe("ai_cost_bias", self._on_bias, replay=True)

    # -- Core callbacks ------------------------------------------------------
    def _on_key(self, entity: int, name: str, value: Any, source: Optional[str]) -> None:
        if source:
            with self.state_lock:
                self.key_configured[source] = bool(value)

    def _on_bias(self, entity: int, name: str, value: Any, source: Optional[str]) -> None:
        if source and value in ("auto", "cost", "quality"):
            with self.state_lock:
                self.cost_bias[source] = value

    def _on_response(self, entity: int, name: str, value: Any, source: Optional[str]) -> None:
        if entity != self.entity or not isinstance(value, dict):
            return
        request_id = value.get("request_id")
        if not isinstance(request_id, str):
            return
        text, error, model = value.get("text"), value.get("error"), value.get("model")
        usage = None
        raw_usage = value.get("usage")
        if isinstance(raw_usage, dict):  # reported by ai-test versions that forward Gemini's usageMetadata
            usage = {
                "prompt": int(raw_usage.get("prompt_tokens") or 0),
                "output": int(raw_usage.get("output_tokens") or 0),
                "total": int(raw_usage.get("total_tokens") or 0),
            }
        with self.state_lock:
            event = self.pending.get(request_id)
            if event is None:
                return
            self.replies[request_id] = AiReply(
                text=text if isinstance(text, str) else None,
                error=error if isinstance(error, str) else None,
                model=model if isinstance(model, str) else None,
                usage=usage,
            )
        event.set()

    # -- status --------------------------------------------------------------
    def _running_ai_launches(self) -> list[str]:
        try:
            return [r["launch_id"] for r in self.world.list_running() if r.get("plugin_type") == "ai-test"]
        except Exception as exc:  # noqa: BLE001 - a failed query must not break the dialog
            print(f"[{self.log_name}] couldn't list running plugins: {exc!r}", flush=True)
            return []

    def status(self) -> dict:
        try:
            plugins = self.world.list_plugins()
        except Exception as exc:  # noqa: BLE001
            return {"state": "missing", "message": f"Couldn't ask Sotrice about the AI spin: {exc}", "version": None,
                    "key_configured": False, "cost_bias": "auto"}
        info = next((p for p in plugins if p.get("type") == "ai-test"), None)
        if info is None:
            return {"state": "missing", "version": None, "key_configured": False, "cost_bias": "auto",
                    "message": "Photo scan uses the AI spin, which isn't installed. Install \"ai-test\" from the Marketplace, "
                               "then add your Gemini API key in it."}
        version = info.get("version")
        if not L.version_at_least(version, MIN_AI_VERSION):
            return {"state": "old", "version": version, "key_configured": False, "cost_bias": "auto",
                    "message": f"Photo scan needs the AI spin (ai-test) {MIN_AI_VERSION} or newer -- you have {version}. "
                               "Update it in the Marketplace."}
        launches = self._running_ai_launches()
        with self.state_lock:
            known = [self.key_configured[l] for l in launches if l in self.key_configured]
            biases = [self.cost_bias[l] for l in launches if l in self.cost_bias]
        bias = biases[-1] if biases else "auto"
        if launches and known and not any(known):
            return {"state": "no_key", "version": version, "key_configured": False, "cost_bias": bias,
                    "message": "No Gemini API key is set. Open the AI spin (\"ai-test\") in the Toolbox, paste your key under "
                               "\"Gemini API key\" and press Save, then scan again."}
        if not launches:
            return {"state": "idle", "version": version, "key_configured": False, "cost_bias": bias,
                    "message": "The AI spin will be started for the scan."}
        return {"state": "ready", "version": version, "key_configured": bool(known and any(known)), "cost_bias": bias, "message": ""}

    # -- asking --------------------------------------------------------------
    def _ensure_running(self) -> None:
        if self._running_ai_launches():
            return
        try:
            self.world.start_plugin("ai-test")
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Couldn't start the AI spin (ai-test): {exc}") from exc

    def ask(self, prompt: str, attachments: list[dict], task_hint: str, timeout_s: float) -> AiReply:
        with self.lock:
            self._ensure_running()
            request_id = uuid.uuid4().hex
            event = threading.Event()
            with self.state_lock:
                self.pending[request_id] = event
            try:
                self.world.set_attribute(self.entity, "ai_request", {
                    "prompt": prompt, "request_id": request_id, "task_hint": task_hint, "attachments": attachments,
                })
                got = event.wait(timeout_s)
                with self.state_lock:
                    reply = self.replies.pop(request_id, None)
            finally:
                with self.state_lock:
                    self.pending.pop(request_id, None)
            if not got or reply is None:
                return AiReply(text=None, error=f"no answer from the AI within {timeout_s:.0f}s")
            return reply


class Plugin:
    def __init__(self, world: Any, name: str, service: Service, version: str, directory: Path) -> None:
        self.world = world
        self.name = name
        self.service = service
        self.store = service.store
        self.version = version
        self.dir = directory
        self.control = world.create_entity()
        self.started_at = time.time()
        self.reply_lock = threading.Lock()
        self.publish_lock = threading.Lock()
        self.outbox: list[dict] = []
        self.reply_count = 0
        self.seen: dict[str, int] = {}
        self.published_rev = -1

    # -- publishing ----------------------------------------------------------
    def publish_state(self, force: bool = False) -> None:
        with self.publish_lock:
            snap = self.store.snapshot()
            if not force and snap["rev"] == self.published_rev:
                return
            snap["app"] = {"version": self.version}
            self.world.set_attribute(self.control, "latin_state", snap)
            self.published_rev = snap["rev"]

    def reply(self, sid: str, seq: int, **fields: Any) -> None:
        with self.reply_lock:
            self.outbox.append({"sid": sid, "seq": seq, **fields})
            del self.outbox[:-REPLY_RING]
            self.reply_count += 1
            self.world.set_attribute(self.control, "latin_reply", {"n": self.reply_count, "items": list(self.outbox)})

    # -- commands ------------------------------------------------------------
    def on_command(self, entity: int, attribute: str, value: Any, source: Optional[str]) -> None:
        if entity != self.control or not isinstance(value, dict):
            return
        sid = value.get("sid")
        items = value.get("items")
        if not isinstance(sid, str) or not isinstance(items, list):
            return
        for item in sorted((i for i in items if isinstance(i, dict) and isinstance(i.get("seq"), int)), key=lambda i: i["seq"]):
            seq = item["seq"]
            if seq <= self.seen.get(sid, 0):
                continue
            self.seen[sid] = seq
            if len(self.seen) > SEEN_SESSIONS_MAX:
                self.seen.pop(next(iter(self.seen)))
            if item.get("cmd") in HEAVY_COMMANDS:
                self.reply(sid, seq, ok=True, partial=True, stage="received", message="Working...")
                threading.Thread(target=self.run_item, args=(sid, item), daemon=True).start()
            else:
                self.run_item(sid, item)

    def run_item(self, sid: str, item: dict) -> None:
        seq = item["seq"]
        try:
            payload = self.service.handle(item, progress=lambda p: self.reply(sid, seq, ok=True, partial=True, **p))
            self.publish_state()
            self.reply(sid, seq, ok=True, **payload)
        except (CommandError, StoreError) as exc:
            if isinstance(exc, ReadOnlyError):
                print(f"[{self.name}] refused a change (read-only data): {exc}", flush=True)
            self.publish_state()
            self.reply(sid, seq, ok=False, error=str(exc))
        except Exception:  # noqa: BLE001 - one bad command must never kill the plugin
            traceback.print_exc()
            sys.stderr.flush()
            self.reply(sid, seq, ok=False, error="Something went wrong inside the trainer (details are in the log).")

    def start(self) -> None:
        self.world.subscribe("latin_cmd", self.on_command, replay=False)
        self.publish_state(force=True)
        self.world.set_attribute(self.control, "latin_ready", {
            "version": self.version, "data_dir": str(self.dir), "started_at": self.started_at,
        })


def main() -> None:
    from sotrice_client import World

    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory)
    cache = ScanCache(directory)
    with World() as world:
        name = world.identify(PLUGIN_TYPE)
        version = registered_version(world) or read_version()
        ai = CoreAiBridge(world, name)
        service = Service(store, cache, ai, app_version=version)
        plugin = Plugin(world, name, service, version, directory)
        plugin.start()
        for warning in store.warnings:
            print(f"[{name}] WARNING: {warning}", flush=True)
        print(f"[{name}] v{version} ready -- {len(store.snapshot()['verbs'])} verbs, data in {directory}", flush=True)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print(f"[{name}] stopped", flush=True)


if __name__ == "__main__":
    main()
