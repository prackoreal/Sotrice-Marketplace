"""transit-sim — makes city-roads' synthetic underground layer (see
`city_roads.py`'s `load_underground_layer`/`published_road_network`) move:
spawns one shuttle entity per underground line/tunnel it finds in the
published `road_network`, running it back and forth along that line's own
node sequence forever. Built specifically so rendering-test-ui's Grid
(and Scene) views have real moving subway wagons/tunnel cars to render,
not just static dashed track geometry.

Same "ignorance of the producer, discover by attribute name" relationship
every other watch/write plugin in this project has with city-roads — this
file has no idea `klosterneuburg_center_underground.json` exists, no
hardcoded station ids, no assumption there's exactly one subway line. It
just groups the underground edges it's handed by (transport_mode, name)
and walks each group's own graph from one of its endpoints to the other —
so a future area with two subway lines or three tunnels gets a shuttle
each, automatically, no code change.

Each shuttle publishes the exact same `kind`/`agent_type` shape
agent-behavior-test's own vehicle agents already do (`kind="agent"`,
`agent_type="vehicle"`), plus one new attribute, `vehicle_kind`
("subway_wagon" or "car") that rendering-test-ui's SceneDataStore reads
to pick a real subway-wagon/car silhouette instead of the old generic
box. A vehicle agent with no `vehicle_kind` at all (agent-behavior-test's
existing surface vehicles) falls back to the "car" shape — see
sceneDataStore.ts/GridView.tsx/SceneView.tsx.

Registered in plugins/registry.json as "transit-sim".
"""

import math
import time
from collections import defaultdict

from sotrice_client import World

TICK_HZ = 10.0

# Constant cruising speed per underground transport mode -- deliberately
# simple (no accel/decel ramp, unlike agent-behavior-test's own
# AGENT_TYPE_PHYSICS) since each shuttle just ping-pongs a short, fixed
# line forever; a smooth ramp at the two endpoints would be the natural
# next step if the instant direction-reversal ever reads as too abrupt.
SPEED_BY_KIND = {"subway_wagon": 9.0, "car": 4.0}


def _node_point(node: dict) -> tuple[float, float, float]:
    return (float(node["x"]), float(node.get("y", 0.0)), float(node["z"]))


def _group_key(edge: dict) -> tuple[str, str]:
    return (edge.get("transport_mode", "road"), edge.get("name") or "")


def underground_lines(nodes: dict, edges: list) -> list[dict]:
    """Groups every underground edge by (transport_mode, name) and walks
    each group's own small graph into one ordered list of node ids, from
    one endpoint to the other. Each group here is expected to be a
    simple path (no branches/loops) -- that's what
    generate_synthetic_underground.py produces -- so "pick any endpoint
    and walk" is enough; a branching future layer would need a real
    route-planning step instead, not attempted here."""
    groups: dict[tuple[str, str], set] = defaultdict(set)
    adjacency: dict[tuple[str, str], dict] = defaultdict(lambda: defaultdict(set))
    for edge in edges:
        if edge.get("layer") != "underground":
            continue
        frm, to = edge["from"], edge["to"]
        if frm not in nodes or to not in nodes:
            continue
        key = _group_key(edge)
        groups[key].add(frm)
        groups[key].add(to)
        adjacency[key][frm].add(to)
        adjacency[key][to].add(frm)

    lines = []
    for (transport_mode, name), node_ids in groups.items():
        adj = adjacency[(transport_mode, name)]
        endpoints = [n for n in node_ids if len(adj[n]) == 1]
        start = endpoints[0] if endpoints else next(iter(node_ids))
        ordered = [start]
        visited = {start}
        current = start
        while True:
            next_nodes = [n for n in adj[current] if n not in visited]
            if not next_nodes:
                break
            current = next_nodes[0]
            ordered.append(current)
            visited.add(current)
        vehicle_kind = "subway_wagon" if transport_mode == "subway" else "car"
        lines.append({
            "transport_mode": transport_mode,
            "name": name,
            "vehicle_kind": vehicle_kind,
            "path": [_node_point(nodes[n]) for n in ordered],
        })
    return lines


class Shuttle:
    """One entity ping-ponging back and forth along a fixed path of (x,
    y, z) points at a constant speed -- a deliberately simple mover
    (straight-line segments, instant reversal at each end), matching
    this project's own "simplest honest first slice" convention rather
    than a full vehicle-physics simulation.

    Tracked as a single scalar "distance traveled along the path, in the
    current direction" (`progress_m`) plus a `forward` flag that flips
    whenever that scalar would run past either end -- then re-resolved
    into an (x, y, z) point via the path's own cumulative-distance table
    on every call. Simpler and far less error-prone than trying to track
    "which segment, which direction" as two separately-mutated pieces of
    state."""

    def __init__(self, path: list[tuple[float, float, float]], speed_mps: float):
        self.path = path
        self.speed_mps = speed_mps
        self.forward = True
        self.progress_m = 0.0
        self.cumulative = [0.0]
        for a, b in zip(path, path[1:]):
            self.cumulative.append(self.cumulative[-1] + (math.dist(a, b) or 1e-6))
        self.total_length = self.cumulative[-1] if len(path) > 1 else 0.0

    def _point_at(self, distance: float) -> tuple[float, float, float]:
        distance = min(max(distance, 0.0), self.total_length)
        for i in range(len(self.cumulative) - 1):
            if distance <= self.cumulative[i + 1] or i == len(self.cumulative) - 2:
                seg_length = self.cumulative[i + 1] - self.cumulative[i]
                t = (distance - self.cumulative[i]) / seg_length if seg_length > 1e-9 else 0.0
                a, b = self.path[i], self.path[i + 1]
                return tuple(a[k] + (b[k] - a[k]) * t for k in range(3))
        return self.path[0]

    def step(self, dt: float) -> tuple[float, float, float]:
        if len(self.path) < 2 or self.total_length <= 0.0:
            return self.path[0] if self.path else (0.0, 0.0, 0.0)
        delta = self.speed_mps * dt
        self.progress_m += delta if self.forward else -delta
        if self.progress_m >= self.total_length:
            self.progress_m = self.total_length
            self.forward = False
        elif self.progress_m <= 0.0:
            self.progress_m = 0.0
            self.forward = True
        return self._point_at(self.progress_m)


def main():
    with World() as world:
        name = world.identify("transit-sim")
        print(f"[{name}] waiting for city-roads' road_network to find an underground layer to animate")

        shuttles: dict[int, Shuttle] = {}

        def on_road_network(entity, attribute, value, source):
            lines = underground_lines(value.get("nodes", {}), value.get("edges", []))
            if not lines:
                print(f"[{name}] no underground lines/tunnels in the current road_network yet — idle")
                return
            if shuttles:
                return  # already spawned for an earlier publish; road_network is treated as static per-run, same as every other data-source consumer here
            for line in lines:
                shuttle_entity = world.create_entity()
                world.set_attribute(shuttle_entity, "kind", "agent")
                world.set_attribute(shuttle_entity, "agent_type", "vehicle")
                world.set_attribute(shuttle_entity, "vehicle_kind", line["vehicle_kind"])
                shuttles[shuttle_entity] = Shuttle(line["path"], SPEED_BY_KIND[line["vehicle_kind"]])
            print(f"[{name}] spawned {len(shuttles)} shuttle(s): "
                  + ", ".join(f"{l['vehicle_kind']} on {l['name'] or l['transport_mode']!r}" for l in lines))

        world.subscribe("road_network", on_road_network, replay=True)

        print(f"[{name}] running at {TICK_HZ}Hz — Ctrl+C to stop")
        dt = 1.0 / TICK_HZ
        try:
            while True:
                for entity, shuttle in shuttles.items():
                    x, y, z = shuttle.step(dt)
                    world.set_attribute(entity, "position", {"x": x, "y": y, "z": z})
                time.sleep(dt)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
