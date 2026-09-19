"""Python client for the Sotrice Core server.

Talks to sotrice-server over TCP using one JSON object per line in each
direction. Nothing here is Sotrice-specific magic — any language that can
open a socket and read/write JSON can do exactly this, by design.

Every incoming line has a "kind": "response" (a direct reply to something
we sent) or "push" (unsolicited, from a subscription, arriving whenever it
arrives). A background thread reads every line and sorts it into the
right place — responses go to whichever call is waiting for one, pushes
get handed off to a second background thread that runs the registered
callback — so a plugin that never calls `subscribe` never has to think
about this at all; it still just gets one response per request, in
order, exactly as before.

The read and dispatch steps are deliberately two separate threads, not
one. A push callback is ordinary plugin code — city_routing.py's, for
instance, calls `world.set_attribute(...)` right from inside its
"desired_destination" handler, and that is meant to work, since
set+subscribe reacting to set+subscribe is the entire cooperation model.
That call blocks on a response arriving on `_responses`, which only the
read thread can ever deliver. Dispatching a push straight from the read
thread would mean that same thread is both the ONLY reader capable of
producing that response AND the thing sitting blocked waiting to
consume it — a guaranteed self-deadlock the instant any push handler
makes a request, permanent for the rest of the connection since the
read loop never runs again to read anything, response or push, after
that. Handing pushes to a dedicated dispatch thread (its own FIFO
queue, so pushes are still delivered in the exact order they arrived)
keeps the read thread free to keep reading — including the response a
handler's own request is waiting on — while the dispatch thread is the
one that blocks.

Usage:

    from sotrice_client import World

    with World() as world:
        house = world.create_entity()
        world.set_attribute(house, "square_footage", 1500)
        print(world.get_attribute(house, "square_footage"))  # 1500

        # Subscribe to a name, not an entity — no need to know what
        # exists. Fires for every matching Entity, present or future.
        world.subscribe("square_footage", lambda entity, name, value, source:
            print(f"pushed: entity={entity} {name}={value} (from {source})"))
"""

from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
from typing import Any, Callable, Optional

# When a plugin is launched by the server (see launcher.rs), its stdout
# is a PIPE, not a real console — and on Windows, a piped stdout can
# default to a legacy codepage (e.g. cp1252) instead of UTF-8. Any
# print() containing a character outside that codepage (an em dash, a
# curly quote, an emoji) then raises OSError: [Errno 22] Invalid
# argument and KILLS THE PROCESS — silently, from the server's point of
# view, since by the time it happens the plugin has already done
# whatever it did and just stops mid-run with no further output. This
# hit city_routing.py for real: it ran correctly all the way through
# subscribing to everything, then died the instant it tried to print a
# perfectly ordinary "—" in its own startup message. Forcing UTF-8 here,
# once, for every plugin that imports this module, closes off this
# whole class of bug rather than policing every print() for safe
# characters.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


class SotriceError(RuntimeError):
    """Raised when the server reports a request could not be handled."""


PushCallback = Callable[[int, str, Any, Optional[str]], None]


class World:
    """A connection to a running sotrice-server, and the operations a
    plugin can perform on the Core through it: create entities, set and
    get attributes on them, subscribe to an attribute name across every
    Entity, and read the simulation clock."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = int(os.environ.get("SOTRICE_PLUGIN_PORT", "7878")),
    ) -> None:
        # Mirrors sotrice-server's own SOTRICE_PLUGIN_PORT env var (see
        # server/src/main.rs and ui/app/README.md) — without this, that
        # variable only let you stand up an isolated throwaway server,
        # with no way for any Python plugin to actually connect to it
        # short of hand-editing this default. Explicit `port=` still
        # wins over the env var, same precedence as any other default.
        self._socket = socket.create_connection((host, port))
        self._reader = self._socket.makefile("r", encoding="utf-8", newline="\n")
        self._send_lock = threading.Lock()
        self._responses: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._push_handlers: dict[str, list[PushCallback]] = {}
        # Separate from _reader_thread on purpose — see the module
        # docstring for why dispatching a push on the read thread itself
        # self-deadlocks the moment a handler makes its own request.
        self._push_queue: "queue.Queue[Optional[dict[str, Any]]]" = queue.Queue()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatch_thread.start()
        self.instance_name: Optional[str] = None
        """This connection's own identity, once `identify()` has been
        called — `None` for a connection that never identified itself,
        which is fine; everything else still works anonymously."""

    def __enter__(self) -> "World":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass

    def _read_loop(self) -> None:
        try:
            for line in self._reader:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("kind") == "push":
                    # Handed off, not run here — see the module docstring.
                    self._push_queue.put(message)
                else:
                    self._responses.put(message)
        except OSError:
            pass
        # The connection ended — unblock anyone still waiting on a
        # response instead of hanging forever, and let the dispatch
        # thread finish up rather than blocking on an empty queue.
        self._responses.put({"ok": False, "error": "connection closed"})
        self._push_queue.put(None)

    def _dispatch_loop(self) -> None:
        """Runs on its own thread for the life of the connection, pulling
        pushes off `_push_queue` in the exact order the read loop put them
        there and running each one's callbacks — decoupled from socket
        reading so a callback that calls back into World (a request that
        needs the read thread to deliver its response) can't deadlock."""
        while True:
            message = self._push_queue.get()
            if message is None:  # connection closed, see _read_loop
                break
            self._dispatch_push(message)

    def _dispatch_push(self, message: dict[str, Any]) -> None:
        for handler in self._push_handlers.get(message.get("attribute", ""), []):
            try:
                handler(
                    message["entity"],
                    message["attribute"],
                    message["value"],
                    message.get("source"),
                )
            except Exception as exc:  # noqa: BLE001 - a broken callback must not kill delivery to the others
                print(f"sotrice_client: push callback raised {exc!r}, ignoring")

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._send_lock:
            line = json.dumps(payload) + "\n"
            self._socket.sendall(line.encode("utf-8"))
            response = self._responses.get()

        if not response.get("ok", False):
            raise SotriceError(response.get("error", "unknown error"))
        return response

    def identify(self, plugin_type: str, instance_id: Optional[str] = None) -> str:
        """Declares this connection's plugin type (e.g. "wayfinding").
        The server assigns back a unique instance name for this specific
        connection (e.g. "wayfinding-1", or "wayfinding-2" if another
        instance of the same type is already connected — duplicates are
        an intended use case, not an error). Every later `set_attribute`
        from this connection is tagged with that instance name as its
        source. Optional — a connection that never identifies stays
        anonymous, which is fine, everything else still works.

        `instance_id`, if given explicitly, is sent as-is and used
        verbatim as the instance name — for a fixture that needs several
        distinct identities from within one process (see
        contradiction_demo.py), where each connection's identity has to
        be chosen by the caller, not derived from the environment.

        Otherwise, if SOTRICE_LAUNCH_ID is set in the environment (set by
        the server when it starts this process via start_plugin), that
        value is used instead of letting the server generate one — this
        makes a plugin's Core identity the same string as the launch id
        the UI already has, so stopping it and despawning whatever it
        drew are the same lookup."""
        chosen_id = instance_id or os.environ.get("SOTRICE_LAUNCH_ID")
        request = {"op": "hello", "plugin_type": plugin_type}
        if chosen_id:
            request["instance_id"] = chosen_id
        self.instance_name = self._request(request)["name"]
        return self.instance_name

    def create_entity(self) -> int:
        """Creates a new Entity and returns its raw id."""
        return self._request({"op": "create_entity"})["entity"]

    def entity_exists(self, entity: int) -> bool:
        return self._request({"op": "entity_exists", "entity": entity})["exists"]

    def set_attribute(self, entity: int, name: str, value: Any) -> None:
        """Sets a named Attribute's value on an Entity. `value` must be
        JSON-representable (numbers, strings, booleans, lists, dicts,
        None, or another entity's raw id) — that's the real cost of a
        language-agnostic wire protocol, not a Sotrice-specific limit."""
        self._request(
            {"op": "set_attribute", "entity": entity, "name": name, "value": value}
        )

    def set_many(self, updates: list[tuple[int, str, Any]]) -> None:
        """Applies many attribute writes in ONE round trip instead of one
        message per value — for a plugin managing a large population
        internally (thousands of agents, say) that needs to report many
        values without paying full per-message overhead for each one.
        `updates` is a list of (entity, name, value) tuples, applied in
        order, exactly as if each had been sent as its own
        `set_attribute` call."""
        self._request(
            {
                "op": "set_many",
                "updates": [
                    {"entity": entity, "name": name, "value": value}
                    for entity, name, value in updates
                ],
            }
        )

    def get_attribute(self, entity: int, name: str) -> Any:
        """Returns the current value, or None if it was never set."""
        return self._request({"op": "get_attribute", "entity": entity, "name": name})[
            "value"
        ]

    def has_attribute(self, entity: int, name: str) -> bool:
        return self._request({"op": "has_attribute", "entity": entity, "name": name})[
            "has"
        ]

    def mute_output(self, plugin: str, attribute: str) -> None:
        """Forbids `plugin` (an instance name, e.g. "wayfinding-1") from
        WRITING `attribute` from now on — its set_attribute calls for that
        exact name are silently dropped server-side; everything else it
        does (reads, subscriptions, writes to other names) is untouched.
        Works even if `plugin` isn't currently connected. Any connection
        can call this, not just the target plugin itself — this is meant
        to be driven by a UI/controller, not by the muted plugin."""
        self._request(
            {"op": "mute_output", "plugin": plugin, "attribute": attribute}
        )

    def unmute_output(self, plugin: str, attribute: str) -> None:
        """Reverses mute_output for that exact (plugin, attribute) pair."""
        self._request(
            {"op": "unmute_output", "plugin": plugin, "attribute": attribute}
        )

    def subscribe(self, name: str, callback: PushCallback, replay: bool = False) -> None:
        """Registers `callback(entity, name, value, source)` to fire every
        time ANY Entity has `name` set, from this point forward —
        including Entities that don't exist yet. No need to know what
        exists first; this is the alternative to an enumeration API,
        which the Core deliberately doesn't have. `source` is the
        instance name of whichever connection set the value, or None if
        it never identified itself. `callback` runs on a background
        thread, not the caller's thread — keep it quick and thread-safe.

        `replay=True` additionally fires the callback once immediately
        for every Entity that already has `name` set right now — the fix
        for "a late-joining viewer sees nothing." Default False, since a
        plugin that only cares about future changes shouldn't pay for a
        replay it didn't ask for."""
        self._push_handlers.setdefault(name, []).append(callback)
        self._request({"op": "subscribe", "name": name, "replay": replay})

    def list_plugins(self) -> list[dict[str, str]]:
        """Plugin types available to start, read fresh from the server's
        plugins/registry.json — each a {"type": ..., "description": ...}."""
        return self._request({"op": "list_plugins"})["plugins"]

    def start_plugin(self, plugin_type: str) -> str:
        """Starts a new instance of a registered plugin type as a real OS
        process and returns a launch id for stopping it later. This is
        what a click on the canvas actually does — a browser can't spawn
        a process itself, so the server does it on the UI's behalf."""
        return self._request({"op": "start_plugin", "plugin_type": plugin_type})[
            "launch_id"
        ]

    def stop_plugin(self, launch_id: str) -> None:
        """Stops a plugin process previously started with start_plugin."""
        self._request({"op": "stop_plugin", "launch_id": launch_id})

    def now(self) -> int:
        """The current simulation tick."""
        return self._request({"op": "now"})["tick"]

    def advance(self) -> int:
        """Moves simulation time forward by one tick, returns the new tick."""
        return self._request({"op": "advance"})["tick"]
