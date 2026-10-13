"""Local road-network regeneration geometry — the real procedural-gen
core behind sandbox-mode "drag a building, roads reshape to make room"
(see the design memory this was built from: a sotrice-code-41 memory
file dated 2026-10-12, `project_city_spin_sandbox_road_regen_design`).

Deliberately bounded, stated up front so it's never mistaken for
game-grade topology regeneration: when a building's new footprint
blocks part of the existing road graph, this module removes the
blocked nodes/edges and reconnects whatever lost a direct connection by
building a small synthetic LOOP ROAD around the building's (inflated)
convex hull, with a spoke from each surviving neighbor to its nearest
point on that loop. Rigid-translation moves only, no rotation. This is
a real, working, geometrically-sensible reconnection — just not an
exact concave-footprint route or a full city-builder-grade regrid.

Synthetic nodes/edges are tagged `"synthetic": True` and given
`lat`/`lon` of `None` — they have no real OSM coordinate and must never
be presented as OpenStreetMap data (this project takes ODbL attribution
seriously elsewhere; don't blur that here).

Stdlib-only (plain tuples/dicts/math), matching this project's existing
"no new dependency, runs under a bare system python" convention.
"""

import math
from typing import Dict, List, Optional, Tuple

Point = Tuple[float, float]


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def point_in_polygon(pt: Point, poly: List[Point]) -> bool:
    """Standard ray-casting point-in-polygon test."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-15) + x1):
            inside = not inside
    return inside


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point, b: Point, p: Point) -> bool:
    return min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9 and \
        min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9


def segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    if d1 == 0 and _on_segment(p3, p4, p1):
        return True
    if d2 == 0 and _on_segment(p3, p4, p2):
        return True
    if d3 == 0 and _on_segment(p1, p2, p3):
        return True
    if d4 == 0 and _on_segment(p1, p2, p4):
        return True
    return False


def segment_intersects_polygon(p1: Point, p2: Point, poly: List[Point]) -> bool:
    if point_in_polygon(p1, poly) or point_in_polygon(p2, poly):
        return True
    n = len(poly)
    for i in range(n):
        if segments_intersect(p1, p2, poly[i], poly[(i + 1) % n]):
            return True
    return False


def convex_hull(points: List[Point]) -> List[Point]:
    """Monotone-chain convex hull. Returns vertices in CCW order."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def half(seq):
        hull: List[Point] = []
        for p in seq:
            while len(hull) >= 2 and _orient(hull[-2], hull[-1], p) <= 0:
                hull.pop()
            hull.append(p)
        return hull

    lower = half(pts)
    upper = half(list(reversed(pts)))
    return lower[:-1] + upper[:-1]


def inflate_polygon(hull: List[Point], margin_m: float) -> List[Point]:
    """Simple radial inflation from the centroid — an approximation of a
    true Minkowski offset, good enough for a small clearance margin
    around a convex hull. Documented simplification, not exact."""
    cx = sum(p[0] for p in hull) / len(hull)
    cz = sum(p[1] for p in hull) / len(hull)
    out = []
    for x, z in hull:
        dx, dz = x - cx, z - cz
        d = math.hypot(dx, dz) or 1.0
        out.append((x + dx / d * margin_m, z + dz / d * margin_m))
    return out


def regenerate_around_building(
    nodes: Dict[str, dict],
    edges: List[dict],
    footprint: List[Point],
    entity_tag: str,
    clearance_m: float = 3.0,
) -> dict:
    """Remove graph geometry blocked by `footprint` and reconnect
    whatever lost a connection via a synthetic loop road around the
    footprint's inflated convex hull.

    `nodes`: {node_id: {"x":..., "z":..., ...}} — only x/z are read.
    `edges`: [{"from":..., "to":..., "length_m":..., "name":..., "highway":...}]
    Returns {"removed_node_ids": [...], "removed_edges": [(from,to), ...]
    (both directions of any affected undirected pair), "added_nodes":
    {id: {...}}, "added_edges": [{...}]}.
    """
    removed_node_ids = {
        nid for nid, n in nodes.items() if point_in_polygon((n["x"], n["z"]), footprint)
    }

    undirected_removed: set = set()
    for e in edges:
        a, b = e["from"], e["to"]
        if a in removed_node_ids or b in removed_node_ids:
            undirected_removed.add(frozenset((a, b)))
            continue
        na, nb = nodes.get(a), nodes.get(b)
        if na is None or nb is None:
            continue
        if segment_intersects_polygon((na["x"], na["z"]), (nb["x"], nb["z"]), footprint):
            undirected_removed.add(frozenset((a, b)))

    removed_edges: List[Tuple[str, str]] = []
    reconnect_targets: set = set()
    for pair in undirected_removed:
        a, b = tuple(pair)
        removed_edges.append((a, b))
        removed_edges.append((b, a))
        if a not in removed_node_ids:
            reconnect_targets.add(a)
        if b not in removed_node_ids:
            reconnect_targets.add(b)

    added_nodes: Dict[str, dict] = {}
    added_edges: List[dict] = []

    if reconnect_targets:
        hull = convex_hull(footprint)
        if len(hull) >= 3:
            inflated = inflate_polygon(hull, clearance_m)
        else:
            inflated = footprint

        hull_ids = []
        for i, (hx, hz) in enumerate(inflated):
            hid = f"synth-{entity_tag}-h{i}"
            added_nodes[hid] = {"x": hx, "z": hz, "lat": None, "lon": None, "synthetic": True}
            hull_ids.append(hid)

        n = len(hull_ids)
        for i in range(n):
            a, b = hull_ids[i], hull_ids[(i + 1) % n]
            pa, pb = inflated[i], inflated[(i + 1) % n]
            length = _dist(pa, pb)
            added_edges.append({"from": a, "to": b, "length_m": length, "name": None,
                                 "highway": "synthetic", "synthetic": True})
            added_edges.append({"from": b, "to": a, "length_m": length, "name": None,
                                 "highway": "synthetic", "synthetic": True})

        for target in reconnect_targets:
            tn = nodes.get(target)
            if tn is None:
                continue
            tp = (tn["x"], tn["z"])
            best_id: Optional[str] = None
            best_d = float("inf")
            for hid, (hx, hz) in zip(hull_ids, inflated):
                d = _dist(tp, (hx, hz))
                if d < best_d:
                    best_d = d
                    best_id = hid
            if best_id is not None and best_d > 1e-6:
                added_edges.append({"from": target, "to": best_id, "length_m": best_d,
                                     "name": None, "highway": "synthetic", "synthetic": True})
                added_edges.append({"from": best_id, "to": target, "length_m": best_d,
                                     "name": None, "highway": "synthetic", "synthetic": True})

    return {
        "removed_node_ids": sorted(removed_node_ids),
        "removed_edges": removed_edges,
        "added_nodes": added_nodes,
        "added_edges": added_edges,
    }


def apply_regeneration(nodes: Dict[str, dict], edges: List[dict], record: dict):
    """Apply one regeneration record (as returned by
    `regenerate_around_building`, or reloaded from an overlay file) onto
    a nodes/edges graph in place. Used both right after a live move and
    when replaying persisted overlay records at startup."""
    for nid in record["removed_node_ids"]:
        nodes.pop(nid, None)
    removed_pairs = {frozenset(pair) for pair in record["removed_edges"]}
    edges[:] = [e for e in edges if frozenset((e["from"], e["to"])) not in removed_pairs]
    nodes.update(record["added_nodes"])
    edges.extend(record["added_edges"])
