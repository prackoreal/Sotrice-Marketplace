"""Python client for the Sotrice Core server.

Talks to sotrice-server over TCP. In each direction, the wire carries a
sequence of self-describing frames: an ordinary frame is one JSON object
terminated by "\n" (the original, still-default "JSON Lines" shape —
any language that can open a socket and read/write JSON can do exactly
this). A connection that opts into the binary fast path (see `identify`'s
`wire` parameter) may ALSO receive raw binary frames, each starting with
a single 0x00 byte (never a valid first byte of a JSON document) followed
by a 4-byte little-endian length and that many raw bytes — see
`_FramedReader` and `_decode_binary_batch`. A connection that never opts
in never receives one; the plain JSON-lines shape is completely
unaffected either way, this is purely additive.

Every incoming JSON frame has a "kind": "response" (a direct reply to
something we sent), "push" (unsolicited, from an unbatched subscription,
arriving whenever it arrives), or "push_batch" (unsolicited, from a
subscription that asked to be batched — see `subscribe`'s `batch_ms` —
carrying an "updates" array of the same per-event shape a "push" would
have sent one at a time). A binary frame decodes to the same information
as a "push_batch" would, just more compactly on the wire; either shape
dispatches through the exact same per-name callbacks, so a subscriber
never needs to know or care which one arrived. A background thread reads
every frame and sorts it into the right place — responses go to whichever
call is waiting for one, pushes (batched or not, JSON or binary) get
handed off to a second background thread that runs the registered
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

# Sentinel byte a binary batch frame always starts with — never the first
# byte of any valid JSON document (0x00 is never whitespace, `{`, `[`,
# `"`, a digit, `t`/`f`/`n`, or `-`), so `_FramedReader` can always tell
# the two frame kinds apart with a single byte of lookahead.
_BINARY_FRAME_MARKER = 0x00


class _FramedReader:
    """Reads the connection's own byte stream and yields one decoded frame
    at a time — either `("json", <parsed dict>)` or `("binary", <raw
    bytes>)` — matching the module's own two-frame-kinds docstring.
    Replaces the earlier plain `socket.makefile("r")` line reader:
    that only ever worked because every frame used to be a JSON line, and
    would corrupt (or crash trying to decode invalid UTF-8) the instant a
    binary batch frame's raw bytes hit it, since a text-mode file object
    has no way to know some of its bytes aren't meant to be text at all.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._socket = sock
        self._buffer = bytearray()

    def _fill(self, at_least: int) -> bool:
        """Reads more bytes off the socket into `_buffer` until it holds
        at least `at_least` bytes, or the connection closes. Returns
        whether that target was reached (False means the connection
        closed first)."""
        while len(self._buffer) < at_least:
            chunk = self._socket.recv(65536)
            if not chunk:
                return False
            self._buffer.extend(chunk)
        return True

    def read_frame(self) -> Optional[tuple[str, Any]]:
        """Returns the next frame, or `None` once the connection has
        closed with nothing further to deliver."""
        if not self._fill(1):
            return None
        marker = self._buffer[0]
        if marker == _BINARY_FRAME_MARKER:
            if not self._fill(5):
                return None
            length = int.from_bytes(self._buffer[1:5], "little")
            if not self._fill(5 + length):
                return None
            payload = bytes(self._buffer[5 : 5 + length])
            del self._buffer[: 5 + length]
            return ("binary", payload)

        # Plain JSON line: read until (and including) the next b"\n".
        while True:
            newline_at = self._buffer.find(b"\n")
            if newline_at != -1:
                line = bytes(self._buffer[:newline_at])
                del self._buffer[: newline_at + 1]
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    return self.read_frame()
                try:
                    return ("json", json.loads(text))
                except json.JSONDecodeError:
                    return self.read_frame()
            if not self._fill(len(self._buffer) + 1):
                return None


def _decode_binary_batch(payload: bytes) -> list[dict[str, Any]]:
    """The Python-side decoder for the binary batch format `encode_batch_binary`
    (server/src/main.rs) produces — see that function's own doc comment
    for the exact layout. Returns a list of plain dicts shaped exactly
    like one "push"/`push_batch`-entry each, so the caller never has to
    special-case where an update actually came from."""
    if payload[:4] != b"SPB1":
        raise ValueError("not a recognized binary batch frame")
    pos = 4

    def read_u16() -> int:
        nonlocal pos
        value = int.from_bytes(payload[pos : pos + 2], "little")
        pos += 2
        return value

    def read_u32() -> int:
        nonlocal pos
        value = int.from_bytes(payload[pos : pos + 4], "little")
        pos += 4
        return value

    def read_u64() -> int:
        nonlocal pos
        value = int.from_bytes(payload[pos : pos + 8], "little")
        pos += 8
        return value

    def read_string(length: int) -> str:
        nonlocal pos
        value = payload[pos : pos + length].decode("utf-8")
        pos += length
        return value

    name_count = read_u16()
    names = [read_string(read_u16()) for _ in range(name_count)]
    source_count = read_u16()
    sources = [read_string(read_u16()) for _ in range(source_count)]
    update_count = read_u32()

    updates = []
    for _ in range(update_count):
        entity = read_u64()
        name_index = read_u16()
        source_index = read_u16()
        value_len = read_u32()
        value = json.loads(read_string(value_len))
        source = None if source_index == 0xFFFF else sources[source_index]
        updates.append({"entity": entity, "attribute": names[name_index], "value": value, "source": source})
    return updates


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
        self._reader = _FramedReader(self._socket)
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
            while True:
                frame = self._reader.read_frame()
                if frame is None:
                    break
                kind, payload = frame
                if kind == "binary":
                    try:
                        updates = _decode_binary_batch(payload)
                    except (ValueError, IndexError, UnicodeDecodeError) as exc:
                        print(f"sotrice_client: could not decode binary batch frame ({exc!r}), ignoring")
                        continue
                    # Handed off, not run here — see the module docstring.
                    for update in updates:
                        self._push_queue.put(update)
                    continue

                message = payload
                if message.get("kind") == "push":
                    self._push_queue.put(message)
                elif message.get("kind") == "push_batch":
                    for update in message.get("updates", []):
                        self._push_queue.put(update)
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
        needs the read thread to deliver its response) can't deadlock.
        One entry per (entity, attribute, value, source) update, regardless
        of whether it arrived as its own "push" message or as one entry
        inside a batched "push_batch"/binary frame — see `_read_loop`."""
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

    def identify(self, plugin_type: str, instance_id: Optional[str] = None, wire: Optional[str] = None) -> str:
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
        drew are the same lookup.

        `wire`, if set to `"binary"`, opts this connection into the binary
        fast path for any BATCHED subscription it later registers (see
        `subscribe`'s `batch_ms`) — see the module docstring and
        `_FramedReader` for what that changes. Omitted (the default) means
        every batch this connection receives stays plain JSON, exactly as
        before this existed."""
        chosen_id = instance_id or os.environ.get("SOTRICE_LAUNCH_ID")
        request: dict[str, Any] = {"op": "hello", "plugin_type": plugin_type}
        if chosen_id:
            request["instance_id"] = chosen_id
        if wire:
            request["wire"] = wire
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

    def dump_entity(self, entity: int) -> list[dict]:
        """A debug/inspector-only escape hatch, NOT a discovery mechanism
        an ordinary plugin should use instead of `subscribe` — see that
        method's own doc comment for why "agree on a name, subscribe
        once" is this project's real answer to "what exists". Returns
        every attribute CURRENTLY set on `entity`, each as
        `{"name": ..., "value": ..., "source": ...}` — a one-shot
        snapshot (call again for a live-updating view, there is no
        subscription form of this)."""
        return self._request({"op": "dump_entity", "entity": entity})["attributes"]

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

    def subscribe(
        self,
        name: str,
        callback: PushCallback,
        replay: bool = False,
        batch_ms: Optional[int] = None,
    ) -> None:
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
        replay it didn't ask for.

        `batch_ms`, if set, asks the server to coalesce this
        subscription's pushes into one combined message every `batch_ms`
        milliseconds instead of sending one message per event — built for
        a fast-changing population (hundreds of physics bodies publishing
        position/velocity at 60Hz) where per-event framing overhead
        dominates. `callback` still fires once per individual update, in
        the same order they happened — batching only changes how many
        wire messages that costs, never what the caller sees. Default
        `None` keeps today's exact immediate-push behavior."""
        self._push_handlers.setdefault(name, []).append(callback)
        request: dict[str, Any] = {"op": "subscribe", "name": name, "replay": replay}
        if batch_ms is not None:
            request["batch_ms"] = batch_ms
        self._request(request)

    def list_plugins(self) -> list[dict[str, str]]:
        """Plugin types available to start, read fresh from the server's
        plugins/registry.json — each a {"type": ..., "description": ...}."""
        return self._request({"op": "list_plugins"})["plugins"]

    def list_running(self) -> list[dict[str, Any]]:
        """Every currently-running instance of ANY plugin type — each a
        {"launch_id": ..., "plugin_type": ..., "disabled": ...}. Check
        this BEFORE calling start_plugin for a plugin type your own code
        depends on but doesn't own (e.g. a companion process another
        plugin might already have started) — start_plugin itself has no
        dedup (a type like graphing legitimately supports several
        concurrent instances), so skipping this check risks spawning a
        redundant second instance of something already running."""
        return self._request({"op": "list_running"})["running"]

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
