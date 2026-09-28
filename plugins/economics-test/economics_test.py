"""Economics spin — SKELETON: proves the wiring (watch real building +
road/agent data, compute something real, publish it back), NOT a real
economic model yet. That bigger "click a house, tweak insulation, watch
it react" vision is a later, UI-heavy feature layered on afterward,
paired with whatever eventually lets someone click a building in the
rendering spin — out of scope here on purpose.

Two small, honest, genuinely-computed metrics, published back onto each
building's OWN entity (this spin creates no new entities of its own — it
augments existing ones, the same "another plugin writes back onto an
entity it doesn't own" pattern city-weather/city-routing already use on
city-layout's entities):

1. `building_road_distance_m` / `building_nearest_road_name` — real,
   static, always computable the moment city-roads AND city-buildings are
   both running: straight-line distance from a building's centroid to
   the nearest real road-graph node, plus that road's real name if it
   has one. A plausible real economic proxy (closer to road access is a
   real driver of property value) computed from data that already
   exists — not fabricated.

2. `building_traffic_visits_count` — a real counter: how many times some
   OTHER entity's published `position` has come within `TRAFFIC_RADIUS_M`
   of a building, with a per-(mover, building) cooldown so one lingering
   body doesn't rack up visits every tick. Starts at a real, meaningful
   0 the moment a building is known, same "a bare 0 baseline is itself
   meaningful" convention city_weather.py's own severity field already
   uses. Honest limitation, not hidden: in THIS project's current
   synthetic scene, physics-test/agent-behavior-test's activation radius
   (tens of meters around their own local origin) doesn't reach anywhere
   near where the real Klosterneuburg buildings actually sit (~150-220m
   away, in the SAME coordinate frame) — so this counter will
   legitimately read 0 in an ordinary live run today. The counting logic
   itself is exercised directly (see the module's own tests, run this
   file with `--selftest`) rather than only ever proven by a live run
   that happens to never trigger it.

This is a completely ordinary watch/write relay plugin: discovers
buildings/roads/movers purely by subscribing to attribute NAMES
(`building_position`, `road_network`, `position`) with `replay=True`
where it matters, never by asking city-buildings/city-roads what they
created. Has no idea whether either is even running — if `road_network`
never arrives, this plugin just never computes `building_road_distance_m`
for anyone, which is a completely fine, harmless state, not an error.

Registered in plugins/registry.json as "economics-test".
"""

import math
import threading
import time

from sotrice_client import World

# How close another entity's position must come to a building's centroid
# to count as a "visit" — deliberately generous (whole-building-vicinity,
# not "touching the wall") for a skeleton metric like this.
TRAFFIC_RADIUS_M = 10.0

# Once a given (mover, building) pair counts a visit, it won't count
# again for this long — otherwise a body sitting still near a building
# would rack up one visit per tick forever, which isn't a meaningful
# "traffic" signal.
TRAFFIC_RECOUNT_COOLDOWN_S = 5.0


class EconomicsState:
    """Holds everything the two metrics need, behind one lock — small
    enough (a handful of buildings, a small road graph) that one lock is
    genuinely fine here, unlike sotrice_core's own sharded AttributeStore,
    which exists for a much bigger, contended workload than this."""

    def __init__(self):
        self.lock = threading.Lock()
        self.road_nodes: dict[str, tuple[float, float]] = {}
        self.road_edges: list[dict] = []
        self.buildings: dict[int, tuple[float, float]] = {}
        self.traffic_counts: dict[int, int] = {}
        self.last_counted: dict[tuple[int, int], float] = {}

    def nearest_road(self, position: tuple[float, float]):
        """Returns (distance_m, road_name_or_none), or None if no road
        network is known yet."""
        if not self.road_nodes:
            return None
        nearest_id, nearest_pos = min(
            self.road_nodes.items(),
            key=lambda item: math.hypot(item[1][0] - position[0], item[1][1] - position[1]),
        )
        distance = math.hypot(nearest_pos[0] - position[0], nearest_pos[1] - position[1])
        road_name = next(
            (e.get("name") for e in self.road_edges if e.get("name") and (e["from"] == nearest_id or e["to"] == nearest_id)),
            None,
        )
        return round(distance, 2), road_name

    def register_visit(self, mover_entity: int, mover_position: tuple[float, float], now: float) -> list[tuple[int, int]]:
        """Checks `mover_position` against every known building and
        returns a list of (building_entity, new_count) for any building
        that just earned a fresh visit — the caller publishes those
        outside the lock, matching this class's own no-network-calls-
        while-locked discipline."""
        fresh: list[tuple[int, int]] = []
        for building_entity, building_position in self.buildings.items():
            if math.hypot(mover_position[0] - building_position[0], mover_position[1] - building_position[1]) > TRAFFIC_RADIUS_M:
                continue
            key = (mover_entity, building_entity)
            if now - self.last_counted.get(key, 0.0) < TRAFFIC_RECOUNT_COOLDOWN_S:
                continue
            self.last_counted[key] = now
            count = self.traffic_counts.get(building_entity, 0) + 1
            self.traffic_counts[building_entity] = count
            fresh.append((building_entity, count))
        return fresh


def main():
    with World() as world:
        name = world.identify("economics-test")
        print(f"[{name}] watching city-roads/city-buildings/moving entities")

        state = EconomicsState()

        def on_road_network(entity, attribute, value, source):
            with state.lock:
                state.road_nodes = {nid: (info["x"], info["z"]) for nid, info in value.get("nodes", {}).items()}
                state.road_edges = list(value.get("edges", []))
                known_buildings = list(state.buildings.items())
            for building_entity, position in known_buildings:
                result = state.nearest_road(position)
                if result is None:
                    continue
                distance, road_name = result
                world.set_attribute(building_entity, "building_road_distance_m", distance)
                if road_name:
                    world.set_attribute(building_entity, "building_nearest_road_name", road_name)

        def on_building_position(entity, attribute, value, source):
            position = (float(value["x"]), float(value["z"]))
            with state.lock:
                state.buildings[entity] = position
                state.traffic_counts.setdefault(entity, 0)
                result = state.nearest_road(position)
            # A real, meaningful "zero visits so far" baseline the moment
            # a building is known — the same "a bare 0 is itself
            # meaningful" convention city_weather.py's own severity field
            # already uses, not an omission waiting to be filled in.
            world.set_attribute(entity, "building_traffic_visits_count", state.traffic_counts[entity])
            if result is not None:
                distance, road_name = result
                world.set_attribute(entity, "building_road_distance_m", distance)
                if road_name:
                    world.set_attribute(entity, "building_nearest_road_name", road_name)

        def on_moving_position(entity, attribute, value, source):
            # Any entity's `position` update, from ANY plugin — physics-
            # test's bodies today, whatever else publishes `position`
            # later. A building's own centroid is a SEPARATE attribute
            # name (`building_position`), so this never sees a building
            # "visiting" itself.
            mover_position = (float(value.get("x", 0.0)), float(value.get("z", 0.0)))
            now = time.monotonic()
            with state.lock:
                fresh_visits = state.register_visit(entity, mover_position, now)
            for building_entity, count in fresh_visits:
                world.set_attribute(building_entity, "building_traffic_visits_count", count)

        world.subscribe("road_network", on_road_network, replay=True)
        world.subscribe("building_position", on_building_position, replay=True)
        world.subscribe("position", on_moving_position, replay=False)

        print(f"[{name}] running — Ctrl+C to stop")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


def _selftest():
    """Exercises the traffic-counting logic directly with synthetic
    positions near a building — decoupled from whether THIS project's
    current physics-test/agent-behavior-test scene actually reaches a
    real building's real location (see the module docstring's own note
    on why it currently doesn't). Run with `python economics_test.py
    --selftest` — no server/network needed."""
    state = EconomicsState()
    state.buildings[42] = (100.0, 200.0)

    result = state.nearest_road((100.0, 200.0))
    assert result is None, "no road network known yet must report None, not a fabricated distance"

    state.road_nodes = {"a": (100.0, 190.0), "b": (500.0, 500.0)}
    state.road_edges = [{"from": "a", "to": "b", "name": "Test Street", "length_m": 1.0}]
    distance, road_name = state.nearest_road((100.0, 200.0))
    assert distance == 10.0, f"expected exactly 10m to the nearest node, got {distance}"
    assert road_name == "Test Street", f"expected the real road name, got {road_name!r}"

    now = 1000.0
    visits = state.register_visit(mover_entity=7, mover_position=(100.0, 205.0), now=now)
    assert visits == [(42, 1)], f"expected exactly one fresh visit for building 42, got {visits}"

    # Same mover, still nearby, well within the cooldown — must NOT count again.
    visits_again = state.register_visit(mover_entity=7, mover_position=(101.0, 204.0), now=now + 1.0)
    assert visits_again == [], f"a lingering mover within the cooldown window must not re-count, got {visits_again}"

    # Same mover, after the cooldown elapses — counts again.
    visits_later = state.register_visit(mover_entity=7, mover_position=(100.0, 205.0), now=now + TRAFFIC_RECOUNT_COOLDOWN_S + 0.1)
    assert visits_later == [(42, 2)], f"expected a second visit after the cooldown elapsed, got {visits_later}"

    # A mover far away must never count.
    visits_far = state.register_visit(mover_entity=9, mover_position=(0.0, 0.0), now=now + 100.0)
    assert visits_far == [], f"a mover far outside TRAFFIC_RADIUS_M must never count, got {visits_far}"

    print("economics_test._selftest: all assertions passed")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
