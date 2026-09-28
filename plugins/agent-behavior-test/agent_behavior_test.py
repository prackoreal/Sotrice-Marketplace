"""Agent-behavior spin — SKELETON: the next layer up from the physics
spin, on the same synthetic test scene (physics-test's handful of
falling/settling boxes and spheres). Gives each "full" LOD tier body
autonomous movement: if a real road network is available (see
`city_roads.py`), pathfind along it to a real destination and walk the
actual route, node by node; otherwise fall back to the simplest
legitimate first slice — pick a random nearby point, head toward it, pick
a new one on arrival.

This is a completely ordinary watch/write relay plugin, same shape as
every other plugin in this project: it discovers what exists purely by
subscribing to attribute NAMES (`position`, `physics_lod_tier`,
`road_network`) with `replay=True`, never by asking anything what it
created — the Core has no enumeration API by design, and this file
doesn't need one. It publishes exactly one attribute, `desired_target`,
and has NO idea what (if anything) consumes it — physics_test.py's own
`apply_steering` happens to be the one thing that does today. It also has
no idea whether `city_roads.py` is even running: if `road_network` never
arrives, this plugin just keeps using the random-point fallback forever,
which is a completely fine, harmless state, not an error — the same
graceful-degradation relationship every other optional subscription in
this project already has (see `physics_test.py`'s own `active_viewpoint`
stub).

Also spawns a configurable number of its OWN independent agent entities
(`kind="agent"`, publishing their own `position` directly — no physics
body, no LOD gate, always active), decoupled entirely from physics-test's
fixed synthetic scene — see `configured_agent_count`/
`configured_vehicle_count`/`active_area.json`. This is what actually makes
"pick any real area" generalize: physics-test's handful of demo boxes
doesn't scale to however many agents a real area should have, but this
plugin's own agents do, per-area, via small config values.

Two agent TYPES today (`agent_type`, a separate attribute from `kind`):
"walker" (the original, unhurried pace) and "vehicle" (new — same real
road graph, same pathfinding, just faster, see `AGENT_TYPE_SPEEDS_MPS`) —
proving "different agent kinds move differently along the same real
network" with one real second mode, not an exhaustive multi-modal
transit sim (no buses/bikes tonight, a legitimate later extension).

Registered in plugins/registry.json as "agent-behavior-test" — start it
from the Toolbox alongside physics-test (and, optionally, city-roads),
like any other plugin (see feedback_dogfood_the_ui discipline).
"""

import heapq
import json
import math
import os
import random
import threading
import time

from sotrice_client import World

ACTIVE_AREA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "city_roads_data", "active_area.json")

# Default when active_area.json is missing entirely (or missing the
# agent_count key) — a real, live default, not zero, since spawning this
# plugin's own walker entities is a brand-new capability with no prior
# behavior to stay silently compatible with.
DEFAULT_AGENT_COUNT = 8

# Vehicles are additive: a config with no `vehicle_count` key at all (or
# no active_area.json) spawns zero of them, so old behavior is completely
# unchanged — this is a brand-new second agent type, not a replacement.
DEFAULT_VEHICLE_COUNT = 0

# A real, unhurried walking pace for this plugin's OWN walker entities
# (see `spawn_own_agents`) — separate from `STEER_MAX_SPEED`/
# `STEER_MAX_ACCEL` above, which govern how physics-test nudges ITS OWN
# bodies toward a `desired_target`; these walkers have no physics body at
# all, they just publish a `position` directly, so their speed is a plain
# distance/time constant, no acceleration curve needed.
OWN_AGENT_WALK_SPEED_MPS = 1.4

# A car-like pace for the "vehicle" agent type — same road graph, same
# pathfinding, just faster (roughly city-street car speed, scaled down
# to this small extract's own meter/second units) — proves "different
# agent kinds move differently along the same real network" with one
# real second mode, not a full multi-modal transit simulation.
VEHICLE_WALK_SPEED_MPS = 6.0

# Every kind of own agent this plugin can spawn, and its own speed —
# adding a third mode later (a "cyclist", say) is one more entry here
# plus one more configured-count function, not a structural change.
AGENT_TYPE_SPEEDS_MPS = {
    "walker": OWN_AGENT_WALK_SPEED_MPS,
    "vehicle": VEHICLE_WALK_SPEED_MPS,
}


def configured_agent_count() -> int:
    """How many of THIS plugin's own WALKER entities to spawn — a small,
    editable per-area config value (`city_roads_data/active_area.json`'s
    `agent_count`), not a hardcoded constant, so a bigger or smaller real
    area can get more or fewer agents without touching this file at all.
    Missing entirely (or missing the key) falls back to
    DEFAULT_AGENT_COUNT. Kept under its original key name (`agent_count`,
    not `walker_count`) so an active_area.json written before vehicles
    existed keeps meaning exactly what it always meant."""
    if os.path.exists(ACTIVE_AREA_PATH):
        with open(ACTIVE_AREA_PATH, encoding="utf-8") as f:
            return int(json.load(f).get("agent_count", DEFAULT_AGENT_COUNT))
    return DEFAULT_AGENT_COUNT


def configured_vehicle_count() -> int:
    """How many of THIS plugin's own VEHICLE entities to spawn — same
    `active_area.json`, new `vehicle_count` key. Missing entirely (or
    missing the key) falls back to DEFAULT_VEHICLE_COUNT (0) — a config
    written before vehicles existed spawns none, unchanged behavior."""
    if os.path.exists(ACTIVE_AREA_PATH):
        with open(ACTIVE_AREA_PATH, encoding="utf-8") as f:
            return int(json.load(f).get("vehicle_count", DEFAULT_VEHICLE_COUNT))
    return DEFAULT_VEHICLE_COUNT

# How often this plugin re-evaluates targets — a wander/pathing decision
# doesn't need physics-tick precision (60Hz); a much slower cadence is
# both cheaper and reads as more deliberate "pick a spot, walk there"
# behavior rather than constantly re-aiming.
DECISION_HZ = 5.0

# Random fallback targets (no road network available) are drawn from this
# box around the world origin.
WANDER_HALF_EXTENT = 9.0

# Real road-network destinations are restricted to nodes within this
# radius of the world origin — deliberately a bit smaller than
# physics-test's own FULL_SIM_RADIUS (65 units/meters, see that file),
# so a real road-based journey stays inside the zone physics-test is
# actually steering, rather than picking a destination physics-test
# would stop simulating partway there. A documented coupling, not a
# shared config value — there's no published "what's your full-sim
# radius" attribute yet for this to read instead.
ROAD_DESTINATION_RADIUS = 60.0

# Fraction of the time a NEW destination pick is biased toward a real
# building's own nearest road node (only once `building_position` has
# been observed at all — see `on_building_position`) rather than a
# uniformly random reachable road node. Purely to make agents actually
# walk near real buildings often enough to exercise economics-test's
# building_traffic_visits_count in an ordinary live run — the pure-random
# choice already visits a building's neighborhood sometimes just by
# chance (buildings sit right next to real roads), this just makes it
# reliable rather than lucky.
BUILDING_DESTINATION_BIAS = 0.6

# How close (in the horizontal plane) counts as "arrived" at a waypoint —
# advances to the next one (or picks a new destination) once within this
# distance, rather than orbiting the old one forever as steering's own
# overshoot/correction keeps nudging it.
ARRIVAL_RADIUS = 1.0


def pick_wander_target():
    """The no-road-data fallback: a uniformly random point in a box
    around the origin — see the module docstring."""
    return (
        random.uniform(-WANDER_HALF_EXTENT, WANDER_HALF_EXTENT),
        random.uniform(-WANDER_HALF_EXTENT, WANDER_HALF_EXTENT),
    )


class RoadGraph:
    """A real, minimal routable graph — built once from `city_roads.py`'s
    published `road_network` attribute (see that file's own doc comment
    for the exact shape) and never mutated afterward, since this project
    treats a spin's data source as static for its whole run, same as
    every other one-shot-publish data source here."""

    def __init__(self, nodes: dict, edges: list):
        self.positions: dict[str, tuple[float, float]] = {
            node_id: (info["x"], info["z"]) for node_id, info in nodes.items()
        }
        self.adjacency: dict[str, list[tuple[str, float]]] = {node_id: [] for node_id in nodes}
        undirected: dict[str, set[str]] = {node_id: set() for node_id in nodes}
        for edge in edges:
            frm, to, length = edge["from"], edge["to"], edge["length_m"]
            if frm in self.adjacency and to in self.positions:
                self.adjacency[frm].append((to, length))
                undirected[frm].add(to)
                undirected[to].add(frm)

        # A real, clipped local extract can be genuinely fragmented (see
        # extract_city_roads.py's own note on this) — precomputing which
        # connected component each node belongs to means candidate
        # destinations can be restricted to "reachable from here" up
        # front, instead of discovering a dead end only after Dijkstra
        # already ran and failed.
        self.component_of: dict[str, int] = {}
        component_index = 0
        for node_id in nodes:
            if node_id in self.component_of:
                continue
            stack, component = [node_id], []
            while stack:
                current = stack.pop()
                if current in self.component_of:
                    continue
                self.component_of[current] = component_index
                component.append(current)
                stack.extend(undirected[current] - set(self.component_of))
            component_index += 1

    def nearest_node(self, position: tuple[float, float]) -> str | None:
        if not self.positions:
            return None
        return min(
            self.positions,
            key=lambda node_id: math.hypot(self.positions[node_id][0] - position[0], self.positions[node_id][1] - position[1]),
        )

    def nodes_within(self, center: tuple[float, float], radius: float) -> list[str]:
        return [
            node_id
            for node_id, (x, z) in self.positions.items()
            if math.hypot(x - center[0], z - center[1]) <= radius
        ]

    def reachable_from(self, start: str, candidates: list[str]) -> list[str]:
        """Filters `candidates` down to only those in the same connected
        component as `start` — see the fragmentation note above. A path
        between two nodes in the same component is still not GUARANTEED
        (edge directions/oneway streets could still block it), but it
        rules out the common, cheap-to-detect case up front."""
        wanted_component = self.component_of.get(start)
        return [c for c in candidates if self.component_of.get(c) == wanted_component]

    def shortest_path(self, start: str, goal: str) -> list[str] | None:
        """Textbook Dijkstra — the graph here is small enough (a few
        real city blocks' worth of nodes) that a real A* heuristic would
        be premature optimization; upgrade this if a much larger extract
        ever makes it a real bottleneck, not before."""
        if start == goal:
            return [start]
        distances = {start: 0.0}
        previous: dict[str, str] = {}
        visited = set()
        queue = [(0.0, start)]
        while queue:
            dist, node = heapq.heappop(queue)
            if node in visited:
                continue
            visited.add(node)
            if node == goal:
                break
            for neighbor, length in self.adjacency.get(node, []):
                new_dist = dist + length
                if new_dist < distances.get(neighbor, math.inf):
                    distances[neighbor] = new_dist
                    previous[neighbor] = node
                    heapq.heappush(queue, (new_dist, neighbor))

        if goal not in distances:
            return None
        path = [goal]
        while path[-1] != start:
            path.append(previous[path[-1]])
        path.reverse()
        return path


def main():
    with World() as world:
        name = world.identify("agent-behavior-test")
        print(f"[{name}] watching physics-test's scene for full-tier bodies to move")

        state_lock = threading.Lock()
        # entity -> latest known (x, z) position
        positions: dict[int, tuple[float, float]] = {}
        # entity -> latest known LOD tier — only "full" tier bodies get a
        # target; kinematic/sleeping bodies are intentionally left alone,
        # the same activation-range gate physics-test itself uses.
        tiers: dict[int, str] = {}
        road_graph: RoadGraph | None = None
        # entity -> (x, z) — city-buildings' real building positions, if
        # that plugin is running (purely optional, see the module
        # docstring's own ignorance-of-the-producer framing).
        building_positions: dict[int, tuple[float, float]] = {}

        def on_position(entity, attribute, value, source):
            with state_lock:
                positions[entity] = (float(value.get("x", 0.0)), float(value.get("z", 0.0)))

        def on_tier(entity, attribute, value, source):
            with state_lock:
                tiers[entity] = value

        def on_road_network(entity, attribute, value, source):
            nonlocal road_graph
            graph = RoadGraph(value.get("nodes", {}), value.get("edges", []))
            with state_lock:
                road_graph = graph
            print(f"[{name}] real road network loaded: {len(graph.positions)} nodes — pathfinding is now live")

        def on_building_position(entity, attribute, value, source):
            with state_lock:
                building_positions[entity] = (float(value["x"]), float(value["z"]))

        world.subscribe("position", on_position, replay=True)
        world.subscribe("physics_lod_tier", on_tier, replay=True)
        world.subscribe("road_network", on_road_network, replay=True)
        world.subscribe("building_position", on_building_position, replay=True)

        # This plugin's OWN independent agent entities — decoupled from
        # physics-test's fixed synthetic falling-box scene entirely (that
        # scene is a demo of a handful of hardcoded bodies, not something
        # meant to scale to "however many agents a real area should have").
        # Each one just publishes its own ground position directly (no
        # physics body, no LOD gate — it's always "active"), pathing along
        # the real road network the same way `plan_route` already does for
        # physics-test's bodies below. Two agent types today — "walker"
        # (the original) and "vehicle" (new: same road graph, same
        # pathfinding, just faster — see AGENT_TYPE_SPEEDS_MPS) — proving
        # "different kinds move differently along the same real network"
        # with one real second mode rather than a full multi-modal
        # transit sim. How many of each spawn is per-area config, see
        # `configured_agent_count`/`configured_vehicle_count`.
        walker_count = configured_agent_count()
        vehicle_count = configured_vehicle_count()
        own_agents: list[tuple[int, str]] = []  # (entity, agent_type)
        for agent_type, count in (("walker", walker_count), ("vehicle", vehicle_count)):
            for _ in range(count):
                agent_entity = world.create_entity()
                world.set_attribute(agent_entity, "kind", "agent")
                world.set_attribute(agent_entity, "agent_type", agent_type)
                own_agents.append((agent_entity, agent_type))
        print(f"[{name}] spawned {walker_count} walker(s) + {vehicle_count} vehicle(s) of this plugin's own entities (see active_area.json's agent_count/vehicle_count)")

        own_positions: dict[int, tuple[float, float]] = {e: (0.0, 0.0) for e, _ in own_agents}
        own_routes: dict[int, list[tuple[float, float]]] = {}

        # entity -> remaining waypoints to walk, in order, as (x, z) tuples
        routes: dict[int, list[tuple[float, float]]] = {}
        # entity -> the (x, z) this plugin last actually published, so a
        # decision tick that changes nothing doesn't re-publish for no
        # reason.
        published: dict[int, tuple[float, float]] = {}

        def plan_route(position, graph: RoadGraph | None, known_buildings: dict[int, tuple[float, float]]) -> list[tuple[float, float]]:
            if graph is None or not graph.positions:
                return [pick_wander_target()]
            start_node = graph.nearest_node(position)
            # Restricted to (a) within reach of physics-test's own
            # activation radius, so a chosen destination doesn't sit
            # somewhere physics-test would demote the body out of "full"
            # tier (and stop steering it) partway there, AND (b) actually
            # reachable from `start_node` at all — a real, clipped local
            # extract can be genuinely fragmented (see
            # extract_city_roads.py's own note on this).
            nearby = graph.nodes_within((0.0, 0.0), ROAD_DESTINATION_RADIUS)
            candidates = graph.reachable_from(start_node, nearby)
            candidates = [n for n in candidates if n != start_node]
            if not candidates:
                # Nothing else nearby AND reachable — widen to the whole
                # reachable component rather than picking somewhere this
                # body could never actually get to.
                candidates = [n for n in graph.reachable_from(start_node, list(graph.positions)) if n != start_node]
            if not candidates:
                return [pick_wander_target()]

            goal_node = None
            if known_buildings and random.random() < BUILDING_DESTINATION_BIAS:
                # Head toward a real building: its own nearest road node,
                # if that node happens to be one of our reachable
                # candidates — a real destination with a real reason to
                # be picked, not just "some road node."
                building_position = random.choice(list(known_buildings.values()))
                nearest_to_building = min(
                    candidates,
                    key=lambda node_id: math.hypot(graph.positions[node_id][0] - building_position[0], graph.positions[node_id][1] - building_position[1]),
                )
                goal_node = nearest_to_building
            if goal_node is None:
                goal_node = random.choice(candidates)
            path = graph.shortest_path(start_node, goal_node)
            if not path:
                # Same component doesn't guarantee a path exists (oneway
                # streets can still block it) — fall back to the
                # random-point behavior for this one decision rather than
                # getting stuck.
                return [pick_wander_target()]
            return [graph.positions[node_id] for node_id in path]

        print(f"[{name}] running at {DECISION_HZ}Hz — Ctrl+C to stop")
        try:
            while True:
                with state_lock:
                    known_entities = set(positions) & set(tiers)
                    current_positions = dict(positions)
                    current_tiers = dict(tiers)
                    current_graph = road_graph
                    current_buildings = dict(building_positions)

                for entity in known_entities:
                    if current_tiers.get(entity) != "full":
                        # Not (or no longer) in the activation range —
                        # leave whatever route it already has alone
                        # rather than fighting physics-test's own LOD
                        # gate; it simply won't be steered while outside
                        # "full" tier.
                        continue
                    position = current_positions[entity]
                    route = routes.get(entity)

                    if route and math.hypot(route[0][0] - position[0], route[0][1] - position[1]) < ARRIVAL_RADIUS:
                        route.pop(0)

                    if not route:
                        route = plan_route(position, current_graph, current_buildings)
                        routes[entity] = route

                    next_waypoint = route[0]
                    if published.get(entity) != next_waypoint:
                        published[entity] = next_waypoint
                        world.set_attribute(entity, "desired_target", {"x": next_waypoint[0], "y": 0.0, "z": next_waypoint[1]})

                # This plugin's own agent entities: no physics body, no LOD
                # gate to respect — just move each one directly toward its
                # current waypoint, at a speed that depends on its OWN
                # agent_type (see AGENT_TYPE_SPEEDS_MPS — this is the
                # entire "different kinds move differently" mechanism),
                # and publish the result. Same `plan_route` (road pathing
                # with a building-destination bias, or wander fallback) as
                # the physics-test-steering path above, just applied to
                # positions this plugin owns outright instead of a
                # `desired_target` intent someone else has to interpret.
                for agent_entity, agent_type in own_agents:
                    position = own_positions[agent_entity]
                    route = own_routes.get(agent_entity)

                    if route and math.hypot(route[0][0] - position[0], route[0][1] - position[1]) < ARRIVAL_RADIUS:
                        route.pop(0)
                    if not route:
                        route = plan_route(position, current_graph, current_buildings)
                        own_routes[agent_entity] = route

                    target = route[0]
                    dx, dz = target[0] - position[0], target[1] - position[1]
                    distance = math.hypot(dx, dz)
                    step_distance = AGENT_TYPE_SPEEDS_MPS[agent_type] / DECISION_HZ
                    if distance <= step_distance or distance < 1e-6:
                        new_position = target
                    else:
                        new_position = (position[0] + dx / distance * step_distance, position[1] + dz / distance * step_distance)
                    own_positions[agent_entity] = new_position
                    world.set_attribute(agent_entity, "position", {"x": new_position[0], "y": 0.0, "z": new_position[1]})

                time.sleep(1.0 / DECISION_HZ)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
