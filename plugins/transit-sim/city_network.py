"""city_network — geometry + routing on the REAL street network, shared by
every simulation (cars/people in agent_behavior_test.py, trains in
transit_sim.py). Pure Python, no Core, no I/O: feed it the published
`city_road_graph` (docs/city-schema/schema.md) and it gives you

* lane paths: right-hand traffic, one-ways respected, smooth turns through
  junctions, derived from each directed edge's centreline polyline;
* walking paths: sidewalks (offset from the carriageway where the road has
  them), footways/steps/paths, crossings at junctions, and approach legs to
  building entrances;
* turn-aware routing (no U-turns except at dead ends) and walking routing;
* a building index and the invariant checks the simulations run every tick:
  nobody inside a building, no car off its lane, nobody off its corridor.

Frame (as everywhere): metres, x east, z north. "Right of travel" with x east
and z north means: for a heading (hx, hz) the right-hand normal is (hz, -hx).
"""

import heapq
import math
import random
from collections import defaultdict
from typing import Optional

CAR_HIGHWAYS = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service", "road",
}
NO_WALK_HIGHWAYS = {"motorway", "motorway_link"}
LANE_MAX_W = 3.5
SIDEWALK_GAP = 1.0   # walker lateral position beyond the carriageway edge
CORNER_RADIUS_MAX = 14.0


# --- polylines ------------------------------------------------------------------

class Polyline:
    __slots__ = ("pts", "cum", "length")

    def __init__(self, pts: list):
        self.pts = [(float(p[0]), float(p[1])) for p in pts]
        self.cum = [0.0]
        for i in range(1, len(self.pts)):
            self.cum.append(self.cum[-1] + math.hypot(self.pts[i][0] - self.pts[i - 1][0], self.pts[i][1] - self.pts[i - 1][1]))
        self.length = self.cum[-1]

    def _seg(self, s: float) -> int:
        s = max(0.0, min(self.length, s))
        lo, hi = 0, len(self.cum) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= s:
                lo = mid
            else:
                hi = mid
        return lo

    def at(self, s: float) -> tuple[float, float, float, float]:
        """(x, z, hx, hz) at arclength s (heading is the unit tangent)."""
        if len(self.pts) < 2:
            return self.pts[0][0], self.pts[0][1], 1.0, 0.0
        i = self._seg(s)
        a, b = self.pts[i], self.pts[i + 1]
        seg = self.cum[i + 1] - self.cum[i]
        t = 0.0 if seg < 1e-9 else (max(0.0, min(self.length, s)) - self.cum[i]) / seg
        hx, hz = (b[0] - a[0]) / (seg or 1.0), (b[1] - a[1]) / (seg or 1.0)
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, hx, hz

    def project(self, x: float, z: float, hint: Optional[float] = None, window: float = 25.0) -> tuple[float, float, float]:
        """(s, distance, signed lateral) of the nearest point; lateral > 0 = RIGHT of travel.
        With `hint` (the arclength the caller expects) only segments near it are considered, so a
        polyline that doubles back close to itself cannot steal the projection."""
        best = (0.0, 1e18, 0.0)
        lo_i, hi_i = 0, len(self.pts) - 1
        if hint is not None:
            lo_i = max(0, self._seg(hint - window))
            hi_i = min(len(self.pts) - 1, self._seg(hint + window) + 1)
        for i in range(lo_i, hi_i):
            a, b = self.pts[i], self.pts[i + 1]
            dx, dz = b[0] - a[0], b[1] - a[1]
            seg2 = dx * dx + dz * dz
            t = 0.0 if seg2 < 1e-12 else max(0.0, min(1.0, ((x - a[0]) * dx + (z - a[1]) * dz) / seg2))
            px, pz = a[0] + dx * t, a[1] + dz * t
            d = math.hypot(x - px, z - pz)
            if d < best[1]:
                seg = math.sqrt(seg2) or 1.0
                cross = (dx / seg) * (z - pz) - (dz / seg) * (x - px)  # >0 = left of travel
                best = (self.cum[i] + t * seg, d, -cross if d > 1e-9 else 0.0)
        return best


def offset_points(pts: list, d: float) -> list:
    """Parallel polyline `d` metres to the RIGHT of travel (negative = left),
    with mitred corners (clamped so tight turns don't spike)."""
    n = len(pts)
    if n < 2 or abs(d) < 1e-9:
        return [tuple(p) for p in pts]
    normals = []
    for i in range(n - 1):
        dx, dz = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        length = math.hypot(dx, dz) or 1.0
        normals.append((dz / length, -dx / length))
    out = []
    for i in range(n):
        if i == 0:
            nx, nz, scale = normals[0][0], normals[0][1], 1.0
        elif i == n - 1:
            nx, nz, scale = normals[-1][0], normals[-1][1], 1.0
        else:
            ax, az = normals[i - 1]
            bx, bz = normals[i]
            mx, mz = ax + bx, az + bz
            ml = math.hypot(mx, mz)
            if ml < 1e-6:
                nx, nz, scale = ax, az, 1.0
            else:
                nx, nz = mx / ml, mz / ml
                cos_half = nx * ax + nz * az
                if cos_half < 0.45:
                    # sharp bend: bevel (each segment's own offset end) instead of a long mitre spike
                    out.append((pts[i][0] + ax * d, pts[i][1] + az * d))
                    out.append((pts[i][0] + bx * d, pts[i][1] + bz * d))
                    continue
                scale = 1.0 / cos_half
        out.append((pts[i][0] + nx * d * scale, pts[i][1] + nz * d * scale))
    return out


def densify(pts: list, step: float = 1.5) -> list:
    out = [pts[0]]
    for i in range(1, len(pts)):
        a, b = out[-1], pts[i]
        dist = math.hypot(b[0] - a[0], b[1] - a[1])
        if dist > step:
            k = int(dist // step)
            for j in range(1, k + 1):
                t = j * step / dist
                out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        if math.hypot(b[0] - out[-1][0], b[1] - out[-1][1]) > 1e-6:
            out.append(b)
    return out


def turn_curve(p0, h0, p1, h1, step: float = 1.0) -> list:
    """Smooth connection from the end of one lane (point p0, heading h0) to the
    start of the next (p1, h1): a quadratic Bezier whose control point is where
    the two tangents meet (clamped), or a straight line if they are parallel."""
    dx, dz = p1[0] - p0[0], p1[1] - p0[1]
    dist = math.hypot(dx, dz)
    if dist < 0.5:
        return []
    det = h0[0] * (-h1[1]) - h0[1] * (-h1[0])
    ctrl = None
    if abs(det) > 0.05:
        # solve p0 + a*h0 = p1 - b*h1
        a = (dx * (-h1[1]) - dz * (-h1[0])) / det
        b = (h0[0] * dz - h0[1] * dx) / det
        # only a SENSIBLE corner: both tangent legs forward and no longer than ~the chord itself
        if a > 0 and b > 0 and a + b < 1.6 * dist + 2.0 and a < CORNER_RADIUS_MAX * 2 and b < CORNER_RADIUS_MAX * 2:
            ctrl = (p0[0] + a * h0[0], p0[1] + a * h0[1])
    if ctrl is None:
        n = max(1, int(dist // step))
        return [(p0[0] + dx * i / n, p0[1] + dz * i / n) for i in range(1, n)]
    est = math.hypot(ctrl[0] - p0[0], ctrl[1] - p0[1]) + math.hypot(p1[0] - ctrl[0], p1[1] - ctrl[1])
    n = max(3, int(est // step))
    out = []
    for i in range(1, n):
        t = i / n
        u = 1 - t
        out.append((u * u * p0[0] + 2 * u * t * ctrl[0] + t * t * p1[0], u * u * p0[1] + 2 * u * t * ctrl[1] + t * t * p1[1]))
    return out


# --- terrain ----------------------------------------------------------------------

class TerrainGrid:
    """Height lookup from the published `city_terrain` payload (heights_b64: int16 LE decimetres)."""

    def __init__(self, payload: Optional[dict]):
        self.ok = False
        if not payload or not payload.get("heights_b64"):
            return
        import base64
        import struct

        raw = base64.b64decode(payload["heights_b64"])
        self.nx, self.nz = payload["nx"], payload["nz"]
        self.h = struct.unpack(f"<{self.nx * self.nz}h", raw)
        self.x0, self.z0, self.dx = payload["x0"], payload["z0"], payload["dx"]
        self.ok = True

    def elev(self, x: float, z: float) -> float:
        if not self.ok:
            return 0.0
        fx, fz = (x - self.x0) / self.dx, (z - self.z0) / self.dx
        i0, j0 = max(0, min(self.nx - 1, int(math.floor(fx)))), max(0, min(self.nz - 1, int(math.floor(fz))))
        i1, j1 = min(self.nx - 1, i0 + 1), min(self.nz - 1, j0 + 1)
        ax, az = max(0.0, min(1.0, fx - i0)), max(0.0, min(1.0, fz - j0))
        h, nx = self.h, self.nx
        return ((h[j0 * nx + i0] * (1 - ax) + h[j0 * nx + i1] * ax) * (1 - az) + (h[j1 * nx + i0] * (1 - ax) + h[j1 * nx + i1] * ax) * az) / 10.0


# --- buildings ----------------------------------------------------------------------

def point_in_ring(x: float, z: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, zi = ring[i][0], ring[i][1]
        xj, zj = ring[j][0], ring[j][1]
        if (zi > z) != (zj > z) and x < (xj - xi) * (z - zi) / (zj - zi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _segments_intersect(ax, az, bx, bz, cx, cz, dx, dz) -> bool:
    """True when segments a-b and c-d share a point (including touching)."""
    def orient(px, pz, qx, qz, rx, rz):
        v = (qx - px) * (rz - pz) - (qz - pz) * (rx - px)
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    def on_seg(px, pz, qx, qz, rx, rz):
        return min(px, qx) - 1e-12 <= rx <= max(px, qx) + 1e-12 and min(pz, qz) - 1e-12 <= rz <= max(pz, qz) + 1e-12

    o1, o2 = orient(ax, az, bx, bz, cx, cz), orient(ax, az, bx, bz, dx, dz)
    o3, o4 = orient(cx, cz, dx, dz, ax, az), orient(cx, cz, dx, dz, bx, bz)
    if o1 != o2 and o3 != o4:
        return True
    return (o1 == 0 and on_seg(ax, az, bx, bz, cx, cz)) or (o2 == 0 and on_seg(ax, az, bx, bz, dx, dz)) or (o3 == 0 and on_seg(cx, cz, dx, dz, ax, az)) or (o4 == 0 and on_seg(cx, cz, dx, dz, bx, bz))


class BuildingIndex:
    """Footprints on a uniform grid: `inside(x, z)` -> building id or None."""

    def __init__(self, buildings: list, cell: float = 60.0):
        self.cell = cell
        self.rings: dict[str, list] = {}
        self.holes: dict[str, list] = {}
        self.bbox: dict[str, tuple] = {}
        self.cells: dict[tuple[int, int], list] = defaultdict(list)
        for b in buildings:
            if b.get("has_parts"):
                continue  # parts replace the outline (OSM rule); parts are indexed themselves
            outer = b["footprint"]["outer"]
            if len(outer) < 3:
                continue
            bid = b["id"]
            self.rings[bid] = outer
            self.holes[bid] = b["footprint"].get("holes", [])
            xs, zs = [p[0] for p in outer], [p[1] for p in outer]
            self.bbox[bid] = (min(xs), min(zs), max(xs), max(zs))
            for i in range(int(min(xs) // cell), int(max(xs) // cell) + 1):
                for j in range(int(min(zs) // cell), int(max(zs) // cell) + 1):
                    self.cells[(i, j)].append(bid)

    def inside(self, x: float, z: float, ignore: Optional[str] = None) -> Optional[str]:
        for bid in self.cells.get((int(x // self.cell), int(z // self.cell)), ()):
            if bid == ignore:
                continue
            bb = self.bbox[bid]
            if x < bb[0] or x > bb[2] or z < bb[1] or z > bb[3]:
                continue
            if point_in_ring(x, z, self.rings[bid]) and not any(point_in_ring(x, z, h) for h in self.holes[bid]):
                return bid
        return None

    def segment_hits(self, ax: float, az: float, bx: float, bz: float) -> Optional[str]:
        """Building id whose footprint the straight segment a-b touches (an endpoint inside, or the
        segment crossing the outline), else None. Exact: point sampling can step over a thin
        corner of a footprint, which is how a walker could end up inside a building."""
        lo_x, hi_x = (ax, bx) if ax <= bx else (bx, ax)
        lo_z, hi_z = (az, bz) if az <= bz else (bz, az)
        seen = set()
        for i in range(int(lo_x // self.cell), int(hi_x // self.cell) + 1):
            for j in range(int(lo_z // self.cell), int(hi_z // self.cell) + 1):
                for bid in self.cells.get((i, j), ()):
                    if bid in seen:
                        continue
                    seen.add(bid)
                    bb = self.bbox[bid]
                    if hi_x < bb[0] or lo_x > bb[2] or hi_z < bb[1] or lo_z > bb[3]:
                        continue
                    ring = self.rings[bid]
                    if point_in_ring(ax, az, ring) or point_in_ring(bx, bz, ring):
                        return bid
                    n = len(ring)
                    for k in range(n):
                        p, q = ring[k], ring[(k + 1) % n]
                        if _segments_intersect(ax, az, bx, bz, p[0], p[1], q[0], q[1]):
                            return bid
        return None

    def nearest_boundary_point(self, bid: str, x: float, z: float) -> tuple[float, float]:
        ring = self.rings[bid]
        best, best_d = ring[0], 1e18
        for i in range(len(ring)):
            a, b = ring[i], ring[(i + 1) % len(ring)]
            dx, dz = b[0] - a[0], b[1] - a[1]
            seg2 = dx * dx + dz * dz
            t = 0.0 if seg2 < 1e-12 else max(0.0, min(1.0, ((x - a[0]) * dx + (z - a[1]) * dz) / seg2))
            px, pz = a[0] + dx * t, a[1] + dz * t
            d = math.hypot(x - px, z - pz)
            if d < best_d:
                best, best_d = (px, pz), d
        return best


# --- the network ------------------------------------------------------------------------

class Network:
    """Directed street graph with geometry. Built from `city_road_graph`."""

    def __init__(self, graph: dict):
        self.nodes = graph["nodes"]
        self.edges: dict[str, dict] = {}
        self.out_edges: dict[str, list[str]] = defaultdict(list)
        self.in_edges: dict[str, list[str]] = defaultdict(list)
        self.reverse_of: dict[str, Optional[str]] = {}
        self.geom: dict[str, Polyline] = {}
        pair: dict[tuple[str, str, str], str] = {}
        for e in graph["edges"]:
            if e.get("mode") not in ("road", "foot"):
                continue
            a, b = e["from"], e["to"]
            if a not in self.nodes or b not in self.nodes:
                continue
            eid = e["id"]
            pts = e.get("pts") or [(self.nodes[a]["x"], self.nodes[a]["z"]), (self.nodes[b]["x"], self.nodes[b]["z"])]
            self.edges[eid] = e
            self.geom[eid] = Polyline(pts)
            self.out_edges[a].append(eid)
            self.in_edges[b].append(eid)
            pair[(a, b, e.get("way"))] = eid
        for eid, e in self.edges.items():
            self.reverse_of[eid] = pair.get((e["to"], e["from"], e.get("way")))
        self.car_edges = {eid for eid, e in self.edges.items() if e["mode"] == "road" and e.get("highway") in CAR_HIGHWAYS}
        self.walk_edges = {eid for eid, e in self.edges.items() if e.get("highway") not in NO_WALK_HIGHWAYS}
        self._scc_cache: Optional[set] = None
        self.lane_scale: dict[str, float] = {}      # eid -> multiplier on the lane offset (validated against buildings)
        self.walk_choice: dict[str, tuple[int, float]] = {}  # eid -> (side, offset scale)
        self.blocked_car: set = set()
        self.blocked_walk: set = set()

    # --- lane geometry ---------------------------------------------------------------

    def edge_total_lanes(self, e: dict) -> int:
        n = max(1, int(e.get("lanes") or 1))
        return n if e.get("oneway") else n * 2

    def lane_width(self, e: dict) -> float:
        total = self.edge_total_lanes(e)
        w = e.get("width_m") or total * 3.25
        return max(2.2, min(LANE_MAX_W, w / total))

    def lane_offset(self, e: dict, lane: int) -> float:
        """Right-of-centreline offset (m) of lane `lane` (0 = innermost, n-1 = rightmost) of a directed edge."""
        n = max(1, int(e.get("lanes") or 1))
        lane = max(0, min(n - 1, lane))
        lw = self.lane_width(e)
        return (lane + 0.5 - n / 2.0) * lw if e.get("oneway") else (lane + 0.5) * lw

    def lane_polyline(self, eid: str, lane: int) -> list:
        e = self.edges[eid]
        return offset_points(self.geom[eid].pts, self.lane_offset(e, lane) * self.lane_scale.get(eid, 1.0))

    # --- spatial queries -----------------------------------------------------------------

    def build_node_grid(self, cell: float = 50.0) -> None:
        self._ncell = cell
        self._ngrid: dict[tuple[int, int], list[str]] = defaultdict(list)
        for nid, n in self.nodes.items():
            self._ngrid[(int(n["x"] // cell), int(n["z"] // cell))].append(nid)

    def nearest_node(self, x: float, z: float, accept=None) -> Optional[str]:
        if not hasattr(self, "_ngrid"):
            self.build_node_grid()
        ci, cj = int(x // self._ncell), int(z // self._ncell)
        best, best_d = None, 1e18
        for r in range(0, 6):
            for i in range(ci - r, ci + r + 1):
                for j in range(cj - r, cj + r + 1):
                    if max(abs(i - ci), abs(j - cj)) != r:
                        continue
                    for nid in self._ngrid.get((i, j), ()):
                        if accept and not accept(nid):
                            continue
                        n = self.nodes[nid]
                        d = math.hypot(n["x"] - x, n["z"] - z)
                        if d < best_d:
                            best, best_d = nid, d
            if best is not None and best_d <= r * self._ncell:
                break
        return best

    # --- connectivity ------------------------------------------------------------------------

    def drivable_core(self, min_size: int = 6) -> set:
        """Nodes of the strongly-connected components (of the directed CAR graph)
        with at least `min_size` nodes — where cars can spawn and never dead-end."""
        if self._scc_cache is not None:
            return self._scc_cache
        adj: dict[str, list[str]] = defaultdict(list)
        radj: dict[str, list[str]] = defaultdict(list)
        for eid in self.car_edges:
            e = self.edges[eid]
            adj[e["from"]].append(e["to"])
            radj[e["to"]].append(e["from"])
        order, seen = [], set()
        for start in list(adj):
            if start in seen:
                continue
            stack = [(start, iter(adj[start]))]
            seen.add(start)
            while stack:
                node, it = stack[-1]
                for nxt in it:
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append((nxt, iter(adj[nxt])))
                        break
                else:
                    order.append(node)
                    stack.pop()
        comp: dict[str, int] = {}
        ci = 0
        for start in reversed(order):
            if start in comp:
                continue
            stack = [start]
            comp[start] = ci
            while stack:
                node = stack.pop()
                for prv in radj[node]:
                    if prv not in comp:
                        comp[prv] = ci
                        stack.append(prv)
            ci += 1
        sizes: dict[int, int] = defaultdict(int)
        for c in comp.values():
            sizes[c] += 1
        self._scc_cache = {n for n, c in comp.items() if sizes[c] >= min_size}
        return self._scc_cache

    # --- routing --------------------------------------------------------------------------------

    def drive_route(self, start_edge: str, goal_node: str, core: Optional[set] = None, max_expansions: int = 20000) -> Optional[list[str]]:
        """Turn-aware shortest-time route (edge sequence) from the END of start_edge to
        goal_node over car edges. No U-turn onto the reverse edge unless it is the only way out."""
        e0 = self.edges[start_edge]
        if e0["to"] == goal_node:
            return [start_edge]
        dist = {start_edge: 0.0}
        prev: dict[str, str] = {}
        heap = [(0.0, start_edge)]
        done = set()
        expansions = 0
        goal_edge = None
        while heap and expansions < max_expansions:
            d, eid = heapq.heappop(heap)
            if eid in done:
                continue
            done.add(eid)
            expansions += 1
            e = self.edges[eid]
            if e["to"] == goal_node:
                goal_edge = eid
                break
            options = [o for o in self.out_edges[e["to"]] if o in self.car_edges]
            rev = self.reverse_of.get(eid)
            non_u = [o for o in options if o != rev]
            for o in (non_u or options):
                oe = self.edges[o]
                speed = max(8.0, (oe.get("maxspeed_kmh") or 30)) / 3.6
                nd = d + oe["length_m"] / speed
                if nd < dist.get(o, 1e18):
                    dist[o] = nd
                    prev[o] = eid
                    heapq.heappush(heap, (nd, o))
        if goal_edge is None:
            return None
        route = [goal_edge]
        while route[-1] != start_edge:
            route.append(prev[route[-1]])
        route.reverse()
        return route

    def walk_route(self, start_node: str, goal_node: str, max_expansions: int = 30000) -> Optional[list[str]]:
        """Shortest walking route as a list of directed edge ids (either direction of a way is fine)."""
        if start_node == goal_node:
            return []
        dist = {start_node: 0.0}
        prev: dict[str, tuple[str, str]] = {}
        heap = [(0.0, start_node)]
        done = set()
        n = 0
        while heap and n < max_expansions:
            d, node = heapq.heappop(heap)
            if node in done:
                continue
            done.add(node)
            n += 1
            if node == goal_node:
                break
            for eid in self.out_edges[node]:
                if eid not in self.walk_edges:
                    continue
                e = self.edges[eid]
                # a mapped zebra crossing costs a little, steps a lot (nobody plans a route over stairs for fun)
                cost = e["length_m"] * (1.6 if e.get("highway") == "steps" else 1.0)
                nd = d + cost
                if nd < dist.get(e["to"], 1e18):
                    dist[e["to"]] = nd
                    prev[e["to"]] = (node, eid)
                    heapq.heappush(heap, (nd, e["to"]))
        if goal_node not in prev:
            return None
        route = []
        cur = goal_node
        while cur != start_node:
            node, eid = prev[cur]
            route.append(eid)
            cur = node
        route.reverse()
        return route

    # --- vehicle path ---------------------------------------------------------------------------

    def vehicle_path(self, route: list[str], lanes: Optional[list[int]] = None, straight_turns: bool = False) -> dict:
        """Dense lane-following path for an edge route. Returns
        {pts: [(x,z)], seg: [edge index per sample], kind: ['lane'|'turn' per sample], limit: [m/s per sample], edges: route}."""
        pts: list[tuple[float, float]] = []
        seg: list[int] = []
        kind: list[str] = []
        limit: list[float] = []
        prev_end = None
        prev_head = None
        for idx, eid in enumerate(route):
            e = self.edges[eid]
            lane = lanes[idx] if lanes and idx < len(lanes) else max(0, int(e.get("lanes") or 1) - 1)
            lp = densify(self.lane_polyline(eid, lane))
            if len(lp) < 2:
                continue
            speed = max(5.0, (e.get("maxspeed_kmh") or 30) * 0.85) / 3.6
            h0 = _heading(lp[0], lp[1])
            if prev_end is not None:
                for p in (_chord(prev_end, lp[0]) if straight_turns else turn_curve(prev_end, prev_head, lp[0], h0)):
                    pts.append(p)
                    seg.append(idx)
                    kind.append("turn")
                    limit.append(min(speed, 5.5))
            for p in lp:
                pts.append(p)
                seg.append(idx)
                kind.append("lane")
                limit.append(speed)
            prev_end, prev_head = lp[-1], _heading(lp[-2], lp[-1])
        return {"pts": pts, "seg": seg, "kind": kind, "limit": limit, "edges": route, "cum": _cum(pts)}

    def safe_vehicle_path(self, route: list[str], buildings: "BuildingIndex", lanes: Optional[list[int]] = None) -> Optional[dict]:
        """A vehicle path guaranteed not to enter a building (smooth turns first, straight
        junction chords second), or None — the caller then picks another route. This is the
        'repair immediately' half of the invariant: a violating path is never started."""
        skip = {eid for eid in route if self.edges[eid].get("tunnel") or self.edges[eid].get("bridge")}
        for straight in (False, True):
            path = self.vehicle_path(route, lanes, straight)
            if len(path["pts"]) >= 2 and not path_inside_building(path, buildings, skip, 0.35):
                return path
        return None

    def safe_walker_path(self, route: list[str], start_node: str, buildings: "BuildingIndex") -> Optional[dict]:
        skip = {eid for eid in route if self.edges[eid].get("tunnel") or self.edges[eid].get("bridge")}
        for prefer in (1, -1):
            path = self.walker_path(route, start_node, prefer)
            if len(path["pts"]) >= 2 and not path_inside_building(path, buildings, skip, 0.35):
                return path
        return None

    # --- walking path ------------------------------------------------------------------------------

    def walker_side(self, e: dict, prefer: int = 1) -> int:
        """+1 = walk on the right of travel, -1 = left, 0 = on the centreline (foot ways). Prefer
        the right (people walk on the right in right-hand-traffic countries) unless only the
        left side has a sidewalk — or validation found only the other side free of buildings."""
        if e["mode"] == "foot":
            return 0
        choice = self.walk_choice.get(e["id"])
        if choice is not None:
            return choice[0]
        right, left = e.get("sidewalk_right"), e.get("sidewalk_left")
        if prefer >= 0:
            return 1 if (right or not left) else -1
        return -1 if (left or not right) else 1

    def walk_offset(self, e: dict, side: int) -> float:
        if e["mode"] == "foot" or side == 0:
            return 0.0
        w = e.get("width_m") or 6.0
        has = e.get("sidewalk_right") if side > 0 else e.get("sidewalk_left")
        scale = self.walk_choice.get(e["id"], (side, 1.0))[1]
        return side * (w / 2.0 + (SIDEWALK_GAP if has else 0.3)) * scale

    def walker_path(self, route: list[str], start_node: str, prefer_side: int = 1) -> dict:
        """Dense walking path: each edge's polyline offset onto its sidewalk (right side by
        default), connected at junctions by short corner/crossing legs. kind: 'walk' | 'cross'."""
        pts: list[tuple[float, float]] = []
        seg: list[int] = []
        kind: list[str] = []
        prev_end = None
        prev_side = None
        for idx, eid in enumerate(route):
            e = self.edges[eid]
            side = self.walker_side(e, prefer_side)
            lp = densify(offset_points(self.geom[eid].pts, self.walk_offset(e, side)), 1.5)
            if len(lp) < 2:
                continue
            if prev_end is not None:
                crossing = prev_side is not None and side != prev_side
                gap = math.hypot(lp[0][0] - prev_end[0], lp[0][1] - prev_end[1])
                if gap > 0.4:
                    n = max(1, int(gap // 1.0))
                    for i in range(1, n + 1):
                        t = i / n
                        pts.append((prev_end[0] + (lp[0][0] - prev_end[0]) * t, prev_end[1] + (lp[0][1] - prev_end[1]) * t))
                        seg.append(idx)
                        kind.append("cross" if (crossing or gap > 9.0) else "corner")
            for p in lp:
                pts.append(p)
                seg.append(idx)
                kind.append("walk")
            prev_end, prev_side = lp[-1], side
        return {"pts": pts, "seg": seg, "kind": kind, "edges": route, "cum": _cum(pts)}

    # --- invariants -----------------------------------------------------------------------------------

    def lane_error(self, eid: str, lane: int, x: float, z: float, s_hint: Optional[float] = None) -> float:
        """How far (m) a point is from where lane `lane` of edge `eid` should be at its own arclength
        (projecting on the CENTRELINE so this check is independent of the offsetting code)."""
        e = self.edges[eid]
        _s, _d, lateral = self.geom[eid].project(x, z, s_hint)
        return abs(lateral - self.lane_offset(e, lane) * self.lane_scale.get(eid, 1.0))

    def validate_against(self, buildings: "BuildingIndex", step: float = 1.0) -> dict:
        """Checks every edge's driving lane / walking line against the building footprints
        and repairs what it can: shift the lane toward the centreline, switch the sidewalk
        side, or finally exclude the edge (a road OSM digitised through a building, a covered
        passage). Returns counts. Call once after building the network; excluded edges leave
        `car_edges` / `walk_edges`, so routing never uses them."""
        def clear(pts: list, skip_tunnel: bool) -> bool:
            pl = Polyline(pts)
            t = 0.0
            while t <= pl.length:
                x, z, _hx, _hz = pl.at(t)
                if buildings.inside(x, z):
                    return False
                t += step
            x, z, _hx, _hz = pl.at(pl.length)
            return not buildings.inside(x, z)

        stats = {"car_ok": 0, "car_shifted": 0, "car_blocked": 0, "walk_ok": 0, "walk_adjusted": 0, "walk_blocked": 0}
        for eid in list(self.car_edges):
            e = self.edges[eid]
            if e.get("tunnel") or e.get("bridge"):
                stats["car_ok"] += 1
                continue
            lane = max(0, int(e.get("lanes") or 1) - 1)
            for scale in (1.0, 0.5, 0.0):
                if clear(offset_points(self.geom[eid].pts, self.lane_offset(e, lane) * scale), False):
                    if scale != 1.0:
                        self.lane_scale[eid] = scale
                        stats["car_shifted"] += 1
                    else:
                        stats["car_ok"] += 1
                    break
            else:
                self.car_edges.discard(eid)
                self.blocked_car.add(eid)
                stats["car_blocked"] += 1
        for eid in list(self.walk_edges):
            e = self.edges[eid]
            if e.get("tunnel") or e.get("bridge") or e["mode"] == "foot":
                if e["mode"] == "foot" and not (e.get("tunnel") or e.get("bridge")) and not clear(list(self.geom[eid].pts), True):
                    self.walk_edges.discard(eid)
                    self.blocked_walk.add(eid)
                    stats["walk_blocked"] += 1
                else:
                    stats["walk_ok"] += 1
                continue
            first = self.walker_side(e)
            for side, scale in ((first, 1.0), (-first, 1.0), (first, 0.6), (-first, 0.6), (first, 0.0)):
                self.walk_choice[eid] = (side, scale)
                if clear(offset_points(self.geom[eid].pts, self.walk_offset(e, side)), True):
                    stats["walk_ok" if (side, scale) == (first, 1.0) else "walk_adjusted"] += 1
                    break
            else:
                self.walk_choice.pop(eid, None)
                self.walk_edges.discard(eid)
                self.blocked_walk.add(eid)
                stats["walk_blocked"] += 1
        self._scc_cache = None
        return stats

    def junction_ok(self, node: str, x: float, z: float) -> bool:
        """A corner/crossing leg stays inside the junction: within the widest incident
        carriageway's half width + 6 m of the node."""
        n = self.nodes[node]
        wmax = max((self.edges[e].get("width_m") or 6.0 for e in self.out_edges[node] + self.in_edges[node]), default=6.0)
        return math.hypot(x - n["x"], z - n["z"]) <= wmax / 2.0 + 6.0

    def walker_corridor_ok(self, eid: str, x: float, z: float) -> bool:
        """Walker on a street edge: within the sidewalk strip (or the carriageway edge) on either
        side; on a foot edge: within its half-width."""
        e = self.edges[eid]
        s_along, dist, lateral = self.geom[eid].project(x, z)
        if s_along < 2.5 or s_along > self.geom[eid].length - 2.5:
            # the last metres of an edge blend into the junction (corner legs): judged by the junction rule
            return self.junction_ok(e["from"] if s_along < 2.5 else e["to"], x, z)
        if e["mode"] == "foot":
            return dist <= (e.get("width_m") or 2.0) / 2.0 + 1.0
        w = (e.get("width_m") or 6.0) / 2.0
        if eid in self.walk_choice and self.walk_choice[eid][1] < 1.0:
            return abs(lateral) <= w + SIDEWALK_GAP + 2.5  # moved toward the road to stay clear of buildings
        if not (e.get("sidewalk_left") or e.get("sidewalk_right")):
            return abs(lateral) <= w + SIDEWALK_GAP + 2.5  # no sidewalk mapped: the verge / road edge is walkable
        return w - 1.5 <= abs(lateral) <= w + SIDEWALK_GAP + 2.5


def _chord(a, b, step: float = 1.0) -> list:
    dist = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(dist // step))
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(1, n)] if dist >= 0.5 else []


def _heading(a, b) -> tuple[float, float]:
    d = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
    return ((b[0] - a[0]) / d, (b[1] - a[1]) / d)


def _cum(pts: list) -> list:
    c = [0.0]
    for i in range(1, len(pts)):
        c.append(c[-1] + math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]))
    return c


def sample(path: dict, s: float) -> tuple[float, float, float, float, int, str]:
    """(x, z, hx, hz, edge_index, kind) at arclength s along a dense path dict."""
    cum, pts = path["cum"], path["pts"]
    if s <= 0:
        i = 0
    elif s >= cum[-1]:
        i = len(pts) - 2
    else:
        lo, hi = 0, len(cum) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if cum[mid] <= s:
                lo = mid
            else:
                hi = mid
        i = lo
    a, b = pts[i], pts[i + 1]
    seg = cum[i + 1] - cum[i]
    t = 0.0 if seg < 1e-9 else max(0.0, min(1.0, (s - cum[i]) / seg))
    h = _heading(a, b)
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, h[0], h[1], path["seg"][i], path["kind"][i]


def path_inside_building(path: dict, buildings: BuildingIndex, skip_edges: set, step: float = 2.0) -> Optional[tuple[float, float, str]]:
    """First (x, z, building id) where the path enters a building, ignoring edges in `skip_edges`
    (real building passages / tunnels pass under buildings legitimately). Exact: every straight
    piece of the polyline is tested against the footprints (`step` is kept for callers but no
    longer matters -- sampling at any step can miss a thin corner)."""
    pts = path["pts"]
    seg = path.get("seg")
    edges = path["edges"]
    for i in range(len(pts) - 1):
        if seg is not None and edges[seg[min(i + 1, len(seg) - 1)]] in skip_edges and edges[seg[i]] in skip_edges:
            continue
        a, b = pts[i], pts[i + 1]
        bid = buildings.segment_hits(a[0], a[1], b[0], b[1])
        if bid:
            return a[0], a[1], bid
    return None
