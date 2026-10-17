"""city_sim — the simulation layer on the REAL street + rail network. Pure Python, no Core
(the plugins agent_behavior_test.py and transit_sim.py own the I/O and call into this), so it
is testable offline on the extracted datasets (test_city_sim.py).

* Cars (`Car`): drive lane paths from city_network (right-hand traffic, one-ways, smooth turns,
  never through buildings), IDM-lite headway to the car ahead on the same lane/edge, speed
  capped by the edge's maxspeed and by the live `agent_type_physics.vehicle` settings, slower
  in turns. Re-plan from where they are when a route ends (no teleporting).
* Walkers (`Walker`): sidewalks + crossings, destinations = real POIs / residential and
  office buildings / subway entrances; they enter buildings (state `inside`, dwell, leave
  again) and subway stations (state `in_station`, reappear at another entrance).
* Trains (`TrainService`): a real route relation (`city_rail.lines[]`): ping-pong along
  `paths[0]`, accelerate / brake into each real stop, dwell at platforms, y underground where
  the line is.
* Invariant guards (`Guards`): every tick, O(1) per agent: no agent inside a building (except
  state `inside`), no car off its lane, no walker off its corridor, no train off its track.
  A violation is repaired immediately and counted (`agent_guard_stats`).

Frame: metres, x east, z north, y up. Heading (hx, hz) is a unit vector in the x/z plane.
"""

import bisect
import math
import random
from collections import defaultdict
from typing import Any, Optional

import city_network as cn

CAR_LEN = 4.5
MIN_GAP = 2.0
LOOKAHEAD_S = 1.5
WALK_ACCEL = 3.0
WALK_DECEL = 4.0
UNDERGROUND_DEPTH_CLAMP_M = -22.0   # data carries -24..-48 m by layer; real tunnels are shallower, clamp for display
LANE_TOLERANCE_M = 0.75
TRAIN_SPEED_MPS = {"subway": 14.0, "light_rail": 14.0, "tram": 8.0, "train": 20.0, "monorail": 12.0, "funicular": 4.0}
TRAIN_ACCEL = {"subway": 1.1, "light_rail": 1.0, "tram": 1.0, "train": 0.7, "monorail": 1.0, "funicular": 0.5}
TRAIN_DECEL = {"subway": 1.2, "light_rail": 1.1, "tram": 1.2, "train": 0.8, "monorail": 1.1, "funicular": 0.6}
VEHICLE_KIND = {"subway": "subway_wagon", "light_rail": "subway_wagon", "tram": "tram", "train": "train", "monorail": "subway_wagon", "funicular": "tram"}
TRAIN_DWELL_S = 25.0


def yaw_quaternion(hx: float, hz: float) -> dict:
    """Rotation about +y taking the model's +z axis onto the (x, z) heading."""
    yaw = math.atan2(hx, hz)
    return {"x": 0.0, "y": math.sin(yaw / 2.0), "z": 0.0, "w": math.cos(yaw / 2.0)}


def edge_z_offset(e: dict) -> float:
    """Vertical offset of a road edge relative to the terrain (tunnels dip, bridges rise)."""
    layer = int(e.get("osm_layer") or 0)
    if e.get("tunnel"):
        return -7.0 * max(1, abs(layer))
    if e.get("bridge"):
        return 5.5 * max(1, abs(layer))
    return 0.0


class GuardStats:
    __slots__ = ("checks", "violations", "repairs", "last")

    def __init__(self):
        self.checks = 0
        self.violations = 0
        self.repairs = 0
        self.last: Optional[str] = None

    def as_dict(self) -> dict:
        return {"checks": self.checks, "violations": self.violations, "repairs": self.repairs, "last": self.last}


# --- the data every agent shares -------------------------------------------------------------

class SimWorld:
    """Network + buildings + terrain + destinations, built once per area (off the sim thread)."""

    def __init__(self, graph: dict, buildings: list, terrain_payload: Optional[dict] = None, pois: Optional[list] = None,
                 rail: Optional[dict] = None, seed: Optional[int] = None):
        self.rng = random.Random(seed)
        self.net = cn.Network(graph)
        self.bidx = cn.BuildingIndex(buildings)
        self.terrain = cn.TerrainGrid(terrain_payload)
        self.net.build_node_grid()
        self.validation = self.net.validate_against(self.bidx)
        self.core = self.net.drivable_core()
        self.core_list = list(self.core)
        self.core_edges = [eid for eid in self.net.car_edges if self.net.edges[eid]["from"] in self.core and self.net.edges[eid]["to"] in self.core]
        self.car_km = sum(self.net.edges[e]["length_m"] for e in self.core_edges) / 1000.0
        self.walk_nodes = self._walk_core()
        self._walk_set = set(self.walk_nodes)
        self.rail = rail or {}
        self.entrances = self._entrances()
        self.destinations = self._destinations(buildings, pois or [])

    # -- construction helpers -----------------------------------------------------------------

    def _walk_core(self) -> list:
        """Nodes of the largest weakly-connected walk component (walkers spawn and route inside it)."""
        net = self.net
        parent: dict[str, str] = {}

        def find(a):
            while parent.setdefault(a, a) != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for eid in net.walk_edges:
            e = net.edges[eid]
            ra, rb = find(e["from"]), find(e["to"])
            if ra != rb:
                parent[ra] = rb
        groups: dict[str, list] = defaultdict(list)
        for n in list(parent):
            groups[find(n)].append(n)
        return max(groups.values(), key=len) if groups else []

    def walk_node_near(self, x: float, z: float, max_dist: float = 60.0) -> Optional[str]:
        nid = self.net.nearest_node(x, z, accept=lambda n: n in self._walk_set)
        if nid is None:
            return None
        n = self.net.nodes[nid]
        return nid if math.hypot(n["x"] - x, n["z"] - z) <= max_dist else None

    def _entrances(self) -> list:
        out = []
        for en in self.rail.get("entrances", []):
            node = self.walk_node_near(en["pos"][0], en["pos"][1], 80.0)
            if node:
                out.append({"pos": (en["pos"][0], en["pos"][1]), "station_id": en.get("station_id"), "node": node, "name": en.get("name")})
        return out

    def _destinations(self, buildings: list, pois: list) -> list:
        dests = []
        seen_b = set()

        def add(kind: str, pos, bid: Optional[str], label: Optional[str] = None):
            node = self.walk_node_near(pos[0], pos[1], 45.0)
            if node:
                dests.append({"kind": kind, "pos": (pos[0], pos[1]), "node": node, "bid": bid, "label": label})

        for p in pois:
            pos = p.get("pos")
            if not pos:
                continue
            bid = self.bidx.inside(pos[0], pos[1])
            kind = p.get("kind") or "poi"
            if kind == "transit":
                add("stop", pos, None, p.get("name"))   # a street-level stop: no building to enter
            else:
                if bid:
                    seen_b.add(bid)
                add(kind, pos, bid, p.get("name"))
        # homes / workplaces: a bounded random sample of buildings so every area has some
        cand = [b for b in buildings if b.get("kind") == "building" and not b.get("has_parts") and b["id"] not in seen_b]
        self.rng.shuffle(cand)
        for b in cand[:400]:
            t = b.get("type")
            kind = "home" if t == "residential" else ("work" if t in ("office", "industrial", "warehouse", "civic") else None)
            if kind:
                add(kind, b["centroid"], b["id"])
        return dests

    # -- helpers shared by agents ----------------------------------------------------------------

    def height(self, x: float, z: float, z_off: float = 0.0) -> float:
        return self.terrain.elev(x, z) + z_off

    def default_counts(self) -> tuple[int, int]:
        """(walkers, vehicles) scaled with the real road length (about 2.5 cars + 4 people per km)."""
        vehicles = int(max(6, min(160, round(self.car_km * 2.5))))
        walkers = int(max(12, min(300, round(self.car_km * 4.0))))
        return walkers, vehicles


# --- cars --------------------------------------------------------------------------------------------

class Car:
    kind = "vehicle"

    def __init__(self, entity: int):
        self.entity = entity
        self.path: Optional[dict] = None
        self.s = 0.0
        self.v = 0.0
        self.estart: list = []
        self.x = self.z = 0.0
        self.hx, self.hz = 1.0, 0.0
        self.pos_edge: Optional[str] = None
        self.level = "surface"
        self.state = "driving"
        self.stuck_s = 0.0

    # -- planning ------------------------------------------------------------------------------------

    def _set_path(self, path: dict, s: float) -> None:
        self.path = path
        self.s = s
        # An edge's coordinate starts at its first LANE point: the turn curve / junction chord that
        # leads into it is path-dependent (smooth curve or straight chord), so counting it made the
        # along-edge position of two cars on the same edge differ by up to a curve length, which
        # broke the headway / leader logic. On the entry curve the coordinate is simply negative.
        first: dict[int, int] = {}
        for i, ei in enumerate(path["seg"]):
            if path["kind"][i] == "lane":
                first.setdefault(ei, i)
        for i, ei in enumerate(path["seg"]):
            first.setdefault(ei, i)
        self.estart = [path["cum"][first[i]] if i in first else path["cum"][-1] for i in range(len(path["edges"]))]
        self.estart.append(path["cum"][-1])

    def spawn(self, sim: SimWorld, occupancy: Optional[dict] = None) -> bool:
        rng = sim.rng
        for _ in range(12):
            start = rng.choice(sim.core_edges)
            goal = rng.choice(sim.core_list)
            route = sim.net.drive_route(start, goal, sim.core)
            if not route or len(route) < 2:
                continue
            path = sim.net.safe_vehicle_path(route, sim.bidx)
            if path is None:
                continue
            # start somewhere on the first edge, leaving room ahead
            end0 = max(i for i, ei in enumerate(path["seg"]) if ei == 0 and path["kind"][i] == "lane")
            s0 = rng.uniform(0.0, 0.8) * path["cum"][end0]
            if occupancy and occupancy.get(route[0]):
                continue   # one car per edge at spawn: never start inside another car
            self._set_path(path, s0)
            self.v = 0.0
            self.sample(sim)
            return True
        return False

    def replan(self, sim: SimWorld) -> bool:
        """Route ended: continue from the end of the last edge toward a new goal, without a jump."""
        rng = sim.rng
        last = self.path["edges"][-1] if self.path else None
        if last is None or last not in sim.net.car_edges:
            return self.spawn(sim)
        for _ in range(8):
            goal = rng.choice(sim.core_list)
            route = sim.net.drive_route(last, goal, sim.core)
            if not route or len(route) < 2:
                continue
            path = sim.net.safe_vehicle_path(route, sim.bidx)
            if path is None:
                continue
            end0 = max(i for i, ei in enumerate(path["seg"]) if ei == 0 and path["kind"][i] == "lane")
            self._set_path(path, path["cum"][end0])
            return True
        return self.spawn(sim)

    # -- motion ----------------------------------------------------------------------------------------

    def edge_index(self) -> int:
        i = bisect.bisect_right(self.path["cum"], self.s) - 1
        i = max(0, min(len(self.path["pts"]) - 1, i))
        return self.path["seg"][i]

    def limit_at(self, s: float) -> float:
        cum = self.path["cum"]
        i = max(0, min(len(cum) - 1, bisect.bisect_right(cum, s) - 1))
        return self.path["limit"][i]

    def sample(self, sim: SimWorld) -> None:
        x, z, hx, hz, ei, kind = cn.sample(self.path, self.s)
        self.x, self.z, self.hx, self.hz = x, z, hx, hz
        self.pos_edge = self.path["edges"][ei]
        e = sim.net.edges[self.pos_edge]
        self.level = "tunnel" if e.get("tunnel") else ("bridge" if e.get("bridge") else "surface")
        self.cur_kind = kind
        self.cur_ei = ei

    def step(self, dt: float, sim: SimWorld, gap: Optional[float], leader_v: float, vmax: float, accel: float, decel: float) -> None:
        if self.path is None:
            return
        if self.s >= self.path["cum"][-1] - 0.5:
            if not self.replan(sim):
                return
        look = self.s + self.v * LOOKAHEAD_S + 6.0
        v0 = min(vmax, self.limit_at(self.s), self.limit_at(look))
        tv = v0
        if gap is not None:
            # safe speed: the speed from which we can still stop inside the free gap (a NEGATIVE free gap,
            # i.e. an overlap after two cars merged, must brake below the leader's speed to open it up --
            # clamping the gap at 0 only ever matched the leader's speed and kept the overlap)
            tv = min(tv, 0.9 * math.sqrt(max(0.0, leader_v * leader_v + 2.0 * decel * (gap - MIN_GAP))))
        if tv > self.v:
            self.v = min(tv, self.v + accel * dt)
        else:
            self.v = max(tv, self.v - decel * dt)
        self.s = min(self.path["cum"][-1], self.s + self.v * dt)
        self.stuck_s = self.stuck_s + dt if self.v < 0.2 else 0.0
        self.sample(sim)


def car_gaps(cars: list) -> dict:
    """entity -> (gap_m, leader_speed) to the nearest car ahead on the same edge or the next one."""
    occ: dict[str, list] = defaultdict(list)
    for c in cars:
        if c.path is None:
            continue
        ei = c.edge_index()
        c._ei = ei
        c._es = c.s - c.estart[ei]
        occ[c.path["edges"][ei]].append(c)
    out = {}
    approach: dict[str, list] = defaultdict(list)    # next edge -> cars about to enter it from another edge
    for c in cars:
        if c.path is None:
            continue
        route = c.path["edges"]
        if c._ei + 1 < len(route):
            rem = c.estart[c._ei + 1] - c.s
            if rem < 25.0:
                approach[route[c._ei + 1]].append((rem, c.entity, c))
    for c in cars:
        if c.path is None:
            continue
        ei = c._ei
        route = c.path["edges"]
        best = None
        if ei + 1 < len(route) and c.stuck_s < 12.0:
            rem = c.estart[ei + 1] - c.s
            for r2, ent2, o in approach.get(route[ei + 1], ()):
                if o is not c and (r2, ent2) < (rem, c.entity) and o._ei != ei and rem - r2 < 10.0:
                    d = rem - r2 - CAR_LEN
                    if best is None or d < best[0]:
                        best = (d, o.v)
        for o in occ.get(route[ei], ()):
            if o is not c and o._es > c._es:
                d = o._es - c._es - CAR_LEN
                if best is None or d < best[0]:
                    best = (d, o.v)
        if best is None and ei + 1 < len(route):
            for o in occ.get(route[ei + 1], ()):
                if o is c:
                    continue
                # only cars that are actually on the same lane geometry ahead (same edge in the same direction)
                d = (c.estart[ei + 1] - c.s) + o._es - CAR_LEN
                if best is None or d < best[0]:
                    best = (d, o.v)
        if best is not None:
            out[c.entity] = best
    return out


# --- walkers ---------------------------------------------------------------------------------------------

class Walker:
    kind = "walker"

    def __init__(self, entity: int):
        self.entity = entity
        self.state = "idle"        # walking | inside | in_station | idle
        self.node: Optional[str] = None
        self.path: Optional[dict] = None
        self.s = 0.0
        self.v = 0.0
        self.timer = 0.0
        self.dest: Optional[dict] = None
        self.x = self.z = 0.0
        self.hx, self.hz = 1.0, 0.0
        self.pos_edge: Optional[str] = None
        self.level = "surface"
        self.cur_kind = "walk"
        self.door: Optional[tuple] = None

    # -- helpers ---------------------------------------------------------------------------------------

    def _join(self, sim: SimWorld, path: dict, frm: tuple, kind: str) -> dict:
        """Prefix a straight leg from the walker's current position to the start of `path`."""
        gap = math.hypot(path["pts"][0][0] - frm[0], path["pts"][0][1] - frm[1])
        if gap < 0.4:
            return path
        n = max(1, int(gap // 1.0))
        pts = [(frm[0] + (path["pts"][0][0] - frm[0]) * i / n, frm[1] + (path["pts"][0][1] - frm[1]) * i / n) for i in range(n)]
        out = {"pts": pts + path["pts"], "seg": [0] * n + path["seg"], "kind": [kind] * n + path["kind"], "edges": path["edges"]}
        out["cum"] = cn._cum(out["pts"])
        return out

    def _clear_leg(self, sim: SimWorld, a: tuple, b: tuple, ignore_last: float = 1.2) -> bool:
        """True when the straight leg a -> b stays out of every footprint (the last `ignore_last`
        metres, where it meets a door, are allowed to touch). Exact segment test."""
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length < 1e-6:
            return sim.bidx.inside(a[0], a[1]) is None
        keep = max(0.0, length - ignore_last) / length
        ex, ez = a[0] + (b[0] - a[0]) * keep, a[1] + (b[1] - a[1]) * keep
        return sim.bidx.segment_hits(a[0], a[1], ex, ez) is None

    def place_at_node(self, sim: SimWorld, node: str) -> None:
        n = sim.net.nodes[node]
        self.node = node
        self.x, self.z = n["x"], n["z"]
        self.path = None
        self.state = "idle"

    def spawn(self, sim: SimWorld) -> bool:
        if not sim.walk_nodes:
            return False
        for _ in range(10):
            self.place_at_node(sim, sim.rng.choice(sim.walk_nodes))
            if sim.bidx.inside(self.x, self.z) is None and self.plan(sim, 0.6):
                return True
        return False

    def pick_destination(self, sim: SimWorld, bias: float) -> Optional[dict]:
        rng = sim.rng
        if sim.entrances and rng.random() < 0.08:
            en = rng.choice(sim.entrances)
            return {"kind": "station", "pos": en["pos"], "node": en["node"], "bid": None, "label": en.get("name"), "entrance": en}
        if sim.destinations and rng.random() < bias:
            return rng.choice(sim.destinations)
        node = rng.choice(sim.walk_nodes)
        n = sim.net.nodes[node]
        return {"kind": "node", "pos": (n["x"], n["z"]), "node": node, "bid": None, "label": None}

    def plan(self, sim: SimWorld, bias: float) -> bool:
        net = sim.net
        for _ in range(8):
            dest = self.pick_destination(sim, bias)
            if dest is None or dest["node"] == self.node:
                continue
            route = net.walk_route(self.node, dest["node"])
            if not route:
                continue
            path = net.safe_walker_path(route, self.node, sim.bidx)
            if path is None:
                continue
            if self.state != "inside" and not self._clear_leg(sim, (self.x, self.z), path["pts"][0], 0.0):
                continue   # the connecting leg would clip a building corner: pick another trip
            path = self._join(sim, path, (self.x, self.z), "door" if self.state == "inside" else "corner")
            self.door = None
            door = None
            end = path["pts"][-1]
            if dest["bid"] and dest["kind"] not in ("stop", "node", "station"):
                door = sim.bidx.nearest_boundary_point(dest["bid"], end[0], end[1])
            elif dest["kind"] == "station":
                door = tuple(dest["pos"])
            if door is not None and math.hypot(door[0] - end[0], door[1] - end[1]) < 80.0 and self._clear_leg(sim, end, door):
                self.door = door
                n = max(1, int(math.hypot(door[0] - end[0], door[1] - end[1]) // 1.0))
                for i in range(1, n + 1):
                    path["pts"].append((end[0] + (door[0] - end[0]) * i / n, end[1] + (door[1] - end[1]) * i / n))
                    path["seg"].append(path["seg"][-1])
                    path["kind"].append("door")
                path["cum"] = cn._cum(path["pts"])
            elif dest["kind"] == "station":
                self.door = end   # no clear leg to the entrance: they go down at the end of the sidewalk
            self.dest = dest
            self.path, self.s, self.state = path, 0.0, "walking"
            self.v = 0.0 if self.v < 0.1 else self.v
            self.sample(sim)
            return True
        self.state = "idle"
        return False

    def sample(self, sim: SimWorld) -> None:
        x, z, hx, hz, ei, kind = cn.sample(self.path, self.s)
        self.x, self.z, self.hx, self.hz = x, z, hx, hz
        self.cur_ei, self.cur_kind = ei, kind
        eid = self.path["edges"][ei]
        self.pos_edge = eid
        e = sim.net.edges[eid]
        self.level = "tunnel" if e.get("tunnel") else ("bridge" if e.get("bridge") else "surface")

    def step(self, dt: float, sim: SimWorld, vmax: float, accel: float, decel: float, bias: float) -> None:
        if self.state in ("inside", "in_station"):
            self.timer -= dt
            if self.timer <= 0.0:
                self._leave(sim, bias)
            return
        if self.state == "idle" or self.path is None:
            if sim.walk_nodes and self.node:
                self.plan(sim, bias)
            return
        total = self.path["cum"][-1]
        remaining = total - self.s
        stopping = self.v * self.v / (2.0 * decel)
        if self.dest and self.dest["kind"] in ("home", "work", "shop", "food", "school", "station", "health", "culture", "service", "lodging") and remaining <= stopping + 0.3:
            self.v = max(0.8, self.v - decel * dt)
        else:
            self.v = min(vmax, self.v + accel * dt)
        self.s = min(total, self.s + self.v * dt)
        self.sample(sim)
        if self.s >= total - 1e-6:
            self._arrive(sim)

    def _arrive(self, sim: SimWorld) -> None:
        dest = self.dest or {}
        end = self.path["pts"][-1]
        self.node = dest.get("node", self.node)
        if self.door and dest.get("kind") != "stop":
            self.x, self.z = self.door
            if dest.get("kind") == "station":
                self.state, self.timer = "in_station", sim.rng.uniform(20.0, 60.0)
            else:
                self.state, self.timer = "inside", sim.rng.uniform(15.0, 90.0)
            self.v = 0.0
            self.path = None
            return
        self.x, self.z = end
        self.path = None
        self.state, self.timer = "idle", 0.0
        self.v = 0.0
        self.node = dest.get("node", self.node)

    def _leave(self, sim: SimWorld, bias: float) -> None:
        if self.state == "in_station" and sim.entrances:
            here = (self.dest or {}).get("entrance")
            others = [e for e in sim.entrances if e is not here and e.get("station_id") != (here or {}).get("station_id")] or sim.entrances
            ex = sim.rng.choice(others)
            self.node, self.x, self.z = ex["node"], ex["pos"][0], ex["pos"][1]
            self.state = "inside"   # reuse the door-leg joining below
        self.state = "inside"
        if not self.plan(sim, bias):
            self.state = "idle"


# --- guards -----------------------------------------------------------------------------------------------

class Guards:
    """Per-tick invariant checks + immediate repair. `check_*` return a reason or None."""

    def __init__(self):
        self.stats = GuardStats()

    def _hit(self, who: str, reason: str) -> None:
        self.stats.violations += 1
        self.stats.last = f"{who}: {reason}"

    @staticmethod
    def _pure_lane(c: "Car") -> bool:
        """False on the one interpolation interval between a lane's last point and the turn curve's first."""
        cum = c.path["cum"]
        i = bisect.bisect_right(cum, c.s) - 1
        kinds = c.path["kind"]
        seg = c.path["seg"]
        return not (i + 1 < len(kinds) and (kinds[i + 1] != "lane" or seg[i + 1] != seg[i]))

    def check_car(self, sim: SimWorld, c: Car) -> Optional[str]:
        self.stats.checks += 1
        eid = c.pos_edge
        e = sim.net.edges.get(eid) if eid else None
        if e is None:
            return "unknown edge"
        passage = bool(e.get("tunnel") or e.get("bridge"))
        if not passage and sim.bidx.inside(c.x, c.z):
            return "inside building"
        if c.cur_kind == "lane" and self._pure_lane(c):
            lane = max(0, int(e.get("lanes") or 1) - 1)
            hint = c.s - c.estart[c.cur_ei]
            err = sim.net.lane_error(eid, lane, c.x, c.z, hint)
            if err > LANE_TOLERANCE_M:
                # on a bend the mitred lane vertex legitimately sits up to ~2 m off the straight-lane projection
                _x, _z, h0x, h0z, _e, _k = cn.sample(c.path, max(0.0, c.s - 3.0))
                _x, _z, h1x, h1z, _e, _k = cn.sample(c.path, min(c.path["cum"][-1], c.s + 3.0))
                straight = h0x * h1x + h0z * h1z > 0.99
                if straight or err > 3.5:
                    return f"off lane by {err:.2f} m"
        return None

    def check_walker(self, sim: SimWorld, w: Walker) -> Optional[str]:
        self.stats.checks += 1
        if w.state in ("inside", "in_station", "idle") or w.path is None:
            return None
        if w.cur_kind == "door":
            return None
        if sim.bidx.inside(w.x, w.z) and w.level == "surface":
            return "inside building"
        eid = w.pos_edge
        if eid is None or eid not in sim.net.edges:
            return "unknown edge"
        if w.cur_kind == "walk" and not sim.net.walker_corridor_ok(eid, w.x, w.z):
            return "off corridor"
        if w.cur_kind in ("corner", "cross") and not sim.net.junction_ok(sim.net.edges[eid]["from"], w.x, w.z):
            return "off junction"
        return None

    def repair(self, sim: SimWorld, agent, bias: float = 0.6) -> None:
        """Snap back onto a valid path: re-spawn on the network (a car mid-route re-plans)."""
        ok = agent.spawn(sim)
        if ok:
            self.stats.repairs += 1


# --- trains ----------------------------------------------------------------------------------------------

class TrainService:
    """One physical train running a real line back and forth, stopping at the real stops."""

    def __init__(self, entity: int, line: dict, offset_frac: float):
        self.entity = entity
        self.mode = line["mode"]
        path = line["paths"][0]
        self.poly = cn.Polyline(path)
        ys = (line.get("paths_y") or [[]])[0]
        zo = (line.get("paths_z_offset_m") or [[]])[0]
        n = len(path)
        self.ys = [ys[i] if i < len(ys) else 0.0 for i in range(n)]
        self.zo = [max(zo[i], UNDERGROUND_DEPTH_CLAMP_M) if i < len(zo) else 0.0 for i in range(n)]
        self.length = self.poly.length
        stops = sorted({round(st["s_m"], 1): st for st in line.get("stops", []) if st.get("s_m") is not None}.items())
        self.stops = [(s, st.get("name")) for s, st in stops if 0.0 <= s <= self.length]
        self.s = max(0.0, min(self.length, offset_frac * self.length))
        self.direction = 1
        self.v = 0.0
        self.dwell = 0.0
        self.vmax = TRAIN_SPEED_MPS.get(self.mode, 12.0)
        self.accel = TRAIN_ACCEL.get(self.mode, 1.0)
        self.decel = TRAIN_DECEL.get(self.mode, 1.2)
        self.x = self.z = 0.0
        self.hx, self.hz = 1.0, 0.0
        self.y_rel = 0.0
        self.stop_name: Optional[str] = None
        self.target: Optional[float] = None
        self.state = "running"
        self.vehicle_kind = VEHICLE_KIND.get(self.mode, "train")
        self.ref = line.get("ref")
        self.colour = line.get("colour")
        self._pt_cum = self.poly.cum
        self.sample()

    def _next_target(self) -> float:
        """Arclength the train is heading for: the next stop in its direction, else the line end."""
        if self.direction > 0:
            for s, _n in self.stops:
                if s > self.s + 0.5:
                    return s
            return self.length
        for s, _n in reversed(self.stops):
            if s < self.s - 0.5:
                return s
        return 0.0

    def sample(self) -> None:
        x, z, hx, hz = self.poly.at(self.s)
        if self.direction < 0:
            hx, hz = -hx, -hz
        self.x, self.z, self.hx, self.hz = x, z, hx, hz
        i = self.poly._seg(self.s)
        a, b = self.poly.cum[i], self.poly.cum[i + 1]
        t = 0.0 if b - a < 1e-9 else (self.s - a) / (b - a)
        j = min(i + 1, len(self.ys) - 1)
        self.y_abs = self.ys[i] + (self.ys[j] - self.ys[i]) * t
        self.y_off = self.zo[i] + (self.zo[j] - self.zo[i]) * t

    def step(self, dt: float) -> None:
        if self.length < 10.0:
            return
        if self.dwell > 0.0:
            self.dwell -= dt
            if self.dwell <= 0.0:
                self.state = "running"
                self.stop_name = None
                self.target = None
            return
        if self.target is None:
            self.target = self._next_target()
        dist = (self.target - self.s) * self.direction
        stopping = self.v * self.v / (2.0 * self.decel)
        if dist <= stopping + 0.5:
            self.v = max(0.8, self.v - self.decel * dt)
        else:
            self.v = min(self.vmax, self.v + self.accel * dt)
        move = self.v * dt
        if move >= dist - 0.05:
            self.s = self.target
            self.v = 0.0
            self.stop_name = next((n for s, n in self.stops if abs(self.target - s) < 0.6), None)
            is_stop = self.stop_name is not None or any(abs(self.target - s) < 0.6 for s, _ in self.stops)
            if self.target <= 1e-6:
                self.direction = 1
            elif self.target >= self.length - 1e-6:
                self.direction = -1
            self.dwell = TRAIN_DWELL_S if is_stop else 8.0
            self.state = "dwelling"
        else:
            self.s += move * self.direction
        self.sample()

    def check(self, guards: Guards) -> Optional[str]:
        """Guard: the published position must lie on the train's own path (within 0.5 m)."""
        guards.stats.checks += 1
        _s, d, _lat = self.poly.project(self.x, self.z, self.s, 40.0)
        if d > 0.5:
            return f"off track by {d:.2f} m"
        return None


def build_train_services(lines: list, create_entity) -> list:
    """One service per (mode, ref): the longest relation; trains per line ~ length / 900 m (1..4)."""
    best: dict[tuple, dict] = {}
    for ln in lines:
        if not ln.get("paths") or not ln["paths"][0] or len(ln["paths"][0]) < 2:
            continue
        key = (ln.get("mode"), ln.get("ref") or ln.get("name"))
        length = cn.Polyline(ln["paths"][0]).length
        if length < 80.0:
            continue
        if key not in best or length > best[key][0]:
            best[key] = (length, ln)
    services = []
    for (mode, _ref), (length, ln) in best.items():
        count = max(1, min(4, int(round(length / 900.0))))
        for k in range(count):
            services.append(TrainService(create_entity(), ln, (k + 0.5) / count))
    return services
