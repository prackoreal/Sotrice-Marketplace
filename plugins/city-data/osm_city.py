"""osm_city — turns raw OpenStreetMap elements (Overpass JSON) into the
Sotrice city dataset (schema v1, see C:/st/city-schema/schema.md and the
copy checked in at docs/city-schema/). Pure functions, stdlib only, no
network: osm_fetch.py gets the bytes, this module understands them.

World frame (same as every existing city plugin): metres in a local tangent
plane around the area origin, x = east, z = NORTH, y = up. y = 0 is the
terrain height at the origin; every feature's `y`/`ys` is terrain height
relative to that (bridges/tunnels add their own `z_offset_m`).

Everything the renderer and the simulations need is derived here ONCE and
carries stable ids:
  roads / graph / rail / buildings / ground / terrain / pois
Every drawable surface (wall, roof, road, sidewalk, ground area) has a
stable `surface_id` and a material slot {material, colour, source,
confidence} so a later "looks" pass can fill or override any of them
without touching geometry.
"""

import math
import re
from collections import defaultdict
from typing import Any, Optional

SCHEMA_VERSION = 1
EARTH_R = 6371008.8
ATTRIBUTION = "\u00a9 OpenStreetMap contributors"
LICENSE = "ODbL 1.0 (https://www.openstreetmap.org/copyright)"


# --- projection ------------------------------------------------------------------

class Projection:
    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self._kx = math.radians(1.0) * EARTH_R * math.cos(math.radians(lat0))
        self._kz = math.radians(1.0) * EARTH_R

    def fwd(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.lon0) * self._kx, (lat - self.lat0) * self._kz

    def inv(self, x: float, z: float) -> tuple[float, float]:
        return self.lat0 + z / self._kz, self.lon0 + x / self._kx


# --- small value parsers ----------------------------------------------------------

_NUM = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)\s*([a-zA-Z'\"]*)")


def parse_length_m(value: Any) -> Optional[float]:
    """OSM length: "12", "12.5 m", "40 ft", "10'6\"" (first number wins)."""
    if value is None:
        return None
    m = _NUM.match(str(value).split(";")[0])
    if not m:
        return None
    number = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    if unit in ("ft", "feet", "'"):
        number *= 0.3048
    elif unit in ("km",):
        number *= 1000.0
    elif unit in ("cm",):
        number /= 100.0
    return number if number > 0 else None


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).split(";")[0].strip()))
    except (TypeError, ValueError):
        return None


_SPEED_WORDS = {"walk": 7, "none": 130, "AT:urban": 50, "AT:rural": 100, "AT:motorway": 130, "DE:urban": 50, "DE:rural": 100, "DE:motorway": 130, "urban": 50, "rural": 100}


def parse_speed_kmh(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if text in _SPEED_WORDS:
        return _SPEED_WORDS[text]
    m = _NUM.match(text)
    if not m:
        return None
    number = float(m.group(1).replace(",", "."))
    if "mph" in m.group(2).lower():
        number *= 1.609344
    return int(round(number))


_CSS_COLOURS = {
    "white": "#ffffff", "black": "#000000", "grey": "#808080", "gray": "#808080", "red": "#ff0000", "green": "#008000",
    "blue": "#0000ff", "yellow": "#ffff00", "orange": "#ffa500", "brown": "#a52a2a", "beige": "#f5f5dc", "pink": "#ffc0cb",
    "purple": "#800080", "cyan": "#00ffff", "silver": "#c0c0c0", "gold": "#ffd700", "maroon": "#800000", "navy": "#000080",
    "tan": "#d2b48c", "ivory": "#fffff0", "cream": "#fffdd0", "darkgrey": "#a9a9a9", "darkgray": "#a9a9a9",
    "lightgrey": "#d3d3d3", "lightgray": "#d3d3d3", "lightblue": "#add8e6", "lightgreen": "#90ee90", "darkgreen": "#006400",
    "darkblue": "#00008b", "darkred": "#8b0000", "salmon": "#fa8072", "khaki": "#f0e68c", "olive": "#808000", "teal": "#008080",
    "lime": "#00ff00", "magenta": "#ff00ff", "violet": "#ee82ee", "turquoise": "#40e0d0", "coral": "#ff7f50",
}


def parse_colour(value: Any) -> Optional[str]:
    """OSM colour (#rgb, #rrggbb, or a CSS name) -> "#rrggbb", else None."""
    if not value:
        return None
    text = str(value).strip().lower().split(";")[0]
    if re.fullmatch(r"#[0-9a-f]{6}", text):
        return text
    if re.fullmatch(r"#[0-9a-f]{3}", text):
        return "#" + "".join(c * 2 for c in text[1:])
    return _CSS_COLOURS.get(text.replace(" ", "").replace("_", ""))


_MATERIALS = {
    "brick": "brick", "bricks": "brick", "concrete": "concrete", "reinforced_concrete": "concrete", "cement_block": "concrete",
    "stone": "stone", "sandstone": "stone", "limestone": "stone", "granite": "stone", "marble": "stone", "natural_stone": "stone",
    "wood": "wood", "timber_framing": "wood", "timber": "wood", "glass": "glass", "metal": "metal", "steel": "metal",
    "aluminium": "metal", "aluminum": "metal", "copper": "metal", "zinc": "metal", "plaster": "stucco", "stucco": "stucco",
    "render": "stucco", "roof_tiles": "tiles", "tile": "tiles", "tiles": "tiles", "slate": "slate", "thatch": "thatch",
    "tar_paper": "tar", "asphalt": "asphalt", "paving_stones": "paving_stones", "sett": "sett", "cobblestone": "cobblestone",
    "gravel": "gravel", "fine_gravel": "gravel", "compacted": "gravel", "dirt": "dirt", "ground": "dirt", "unpaved": "dirt",
    "grass": "grass", "sand": "sand", "paved": "asphalt", "concrete:plates": "concrete", "concrete:lanes": "concrete",
}


def slot(material: Optional[str] = None, colour: Optional[str] = None, source: str = "default", confidence: float = 0.0) -> dict:
    """A material slot. `source`: osm | inferred | default | user | ai | imagery."""
    return {"material": material, "colour": colour, "source": source, "confidence": confidence}


def osm_slot(material_tag: Any, colour_tag: Any, default_material: Optional[str] = None, default_colour: Optional[str] = None) -> dict:
    material = _MATERIALS.get(str(material_tag).strip().lower()) if material_tag else None
    if material_tag and material is None:
        material = str(material_tag).strip().lower()  # unknown tag value: keep it, the looks pass can interpret it
    colour = parse_colour(colour_tag)
    if material or colour:
        return slot(material or default_material, colour or default_colour, "osm", 0.9 if (material and colour) else 0.8)
    return slot(default_material, default_colour, "default", 0.15 if (default_material or default_colour) else 0.0)


def r2(value: float) -> float:
    return round(value, 2)


# --- polygon geometry ----------------------------------------------------------------

def ring_area(ring: list) -> float:
    total = 0.0
    for i in range(len(ring)):
        x0, z0 = ring[i]
        x1, z1 = ring[(i + 1) % len(ring)]
        total += x0 * z1 - x1 * z0
    return total / 2.0


def ring_centroid(ring: list) -> tuple[float, float]:
    area = ring_area(ring)
    if abs(area) < 1e-6:
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    cx = cz = 0.0
    for i in range(len(ring)):
        x0, z0 = ring[i]
        x1, z1 = ring[(i + 1) % len(ring)]
        cross = x0 * z1 - x1 * z0
        cx += (x0 + x1) * cross
        cz += (z0 + z1) * cross
    return cx / (6 * area), cz / (6 * area)


def point_in_ring(x: float, z: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, zi = ring[i]
        xj, zj = ring[j]
        if (zi > z) != (zj > z) and x < (xj - xi) * (z - zi) / (zj - zi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def ring_bbox(ring: list) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    zs = [p[1] for p in ring]
    return min(xs), min(zs), max(xs), max(zs)


def orient(ring: list, ccw: bool) -> list:
    return ring if (ring_area(ring) > 0) == ccw else ring[::-1]


class GridIndex:
    """Uniform grid over bounding boxes: cheap 'which features could contain this point'."""

    def __init__(self, cell: float = 80.0):
        self.cell = cell
        self.cells: dict[tuple[int, int], list] = defaultdict(list)

    def add(self, item, bbox: tuple[float, float, float, float]) -> None:
        c = self.cell
        for i in range(int(bbox[0] // c), int(bbox[2] // c) + 1):
            for j in range(int(bbox[1] // c), int(bbox[3] // c) + 1):
                self.cells[(i, j)].append(item)

    def at(self, x: float, z: float) -> list:
        return self.cells.get((int(x // self.cell), int(z // self.cell)), [])


def assemble_rings(lines: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Stitches multipolygon member polylines (each a list of (lat, lon)) into
    closed rings by matching endpoints. Unclosable leftovers are dropped."""
    lines = [list(line) for line in lines if len(line) >= 2]
    rings: list = []
    while lines:
        ring = lines.pop()
        progressed = True
        while progressed and ring[0] != ring[-1]:
            progressed = False
            for index, other in enumerate(lines):
                if other[0] == ring[-1]:
                    ring.extend(other[1:])
                elif other[-1] == ring[-1]:
                    ring.extend(reversed(other[:-1]))
                elif other[-1] == ring[0]:
                    ring = other[:-1] + ring
                elif other[0] == ring[0]:
                    ring = list(reversed(other[1:])) + ring
                else:
                    continue
                lines.pop(index)
                progressed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring[:-1])
    return rings


# --- terrain --------------------------------------------------------------------------

class Terrain:
    """Bilinear height lookup over the schema's `terrain` grid (metres relative to the origin)."""

    def __init__(self, grid: Optional[dict]):
        self.grid = grid

    def elev(self, x: float, z: float) -> float:
        g = self.grid
        if not g:
            return 0.0
        fx = (x - g["x0"]) / g["dx"]
        fz = (z - g["z0"]) / g["dx"]
        i0 = max(0, min(g["nx"] - 1, int(math.floor(fx))))
        j0 = max(0, min(g["nz"] - 1, int(math.floor(fz))))
        i1, j1 = min(g["nx"] - 1, i0 + 1), min(g["nz"] - 1, j0 + 1)
        ax = max(0.0, min(1.0, fx - i0))
        az = max(0.0, min(1.0, fz - j0))
        h = g["heights_dm"]
        nx = g["nx"]
        top = h[j0 * nx + i0] * (1 - ax) + h[j0 * nx + i1] * ax
        bottom = h[j1 * nx + i0] * (1 - ax) + h[j1 * nx + i1] * ax
        return (top * (1 - az) + bottom * az) / 10.0


# --- roads ----------------------------------------------------------------------------------

CAR_CLASSES = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service", "road",
}
FOOT_CLASSES = {"pedestrian", "footway", "steps", "path", "cycleway", "track", "bridleway"}
SKIP_HIGHWAY = {"proposed", "construction", "raceway", "platform", "elevator", "corridor", "services", "rest_area", "bus_guideway"}
SIDEWALK_DEFAULT = {"primary", "primary_link", "secondary", "secondary_link", "tertiary", "tertiary_link", "residential", "unclassified", "living_street", "trunk"}
DEFAULT_LANES = {"motorway": 4, "motorway_link": 1, "trunk": 4, "trunk_link": 1, "primary": 2, "primary_link": 1, "secondary": 2, "secondary_link": 1,
                 "tertiary": 2, "tertiary_link": 1, "unclassified": 2, "residential": 2, "living_street": 2, "service": 1, "road": 2}
LANE_WIDTH = 3.25
MIN_WIDTH = {"motorway": 7.0, "trunk": 7.0, "primary": 6.5, "secondary": 6.0, "tertiary": 5.5, "unclassified": 5.0, "residential": 5.0,
             "living_street": 4.5, "service": 3.0, "pedestrian": 4.0, "footway": 1.5, "path": 1.2, "steps": 1.5, "cycleway": 2.0, "track": 3.0, "bridleway": 2.0}
DEFAULT_SPEED = {"motorway": 130, "motorway_link": 80, "trunk": 100, "trunk_link": 70, "primary": 50, "primary_link": 40, "secondary": 50,
                 "secondary_link": 40, "tertiary": 50, "tertiary_link": 40, "unclassified": 50, "residential": 30, "living_street": 10, "service": 20, "road": 30}
ROAD_SURFACE_DEFAULT = {"motorway": "asphalt", "trunk": "asphalt", "primary": "asphalt", "secondary": "asphalt", "tertiary": "asphalt",
                        "residential": "asphalt", "unclassified": "asphalt", "service": "asphalt", "living_street": "paving_stones",
                        "pedestrian": "paving_stones", "footway": "paving_stones", "path": "dirt", "track": "gravel", "cycleway": "asphalt", "steps": "concrete"}
SURFACE_COLOUR = {"asphalt": "#4a4a4d", "paving_stones": "#9a958c", "sett": "#8a857b", "cobblestone": "#857f74", "concrete": "#a9a9a6",
                  "gravel": "#a39a88", "dirt": "#7a6a52", "grass": "#5d8a3a"}


def way_level(tags: dict, kind: str) -> dict:
    """bridge/tunnel/layer -> {tunnel, bridge, osm_layer, level, z_offset_m}."""
    tunnel_tag = tags.get("tunnel")
    bridge_tag = tags.get("bridge")
    tunnel = bool(tunnel_tag) and tunnel_tag != "no"
    bridge = bool(bridge_tag) and bridge_tag != "no"
    layer = parse_int(tags.get("layer")) or 0
    if tunnel and layer >= 0 and tunnel_tag not in ("building_passage", "culvert"):
        layer = -1
    if bridge and layer <= 0:
        layer = 1
    if tunnel_tag == "building_passage":
        offset = 0.0
    elif tunnel_tag == "culvert":
        offset = -2.0
    elif tunnel:
        depth = {"subway": 12.0, "rail": 10.0, "light_rail": 10.0, "tram": 8.0}.get(kind, 7.0)
        offset = -depth * max(1, -layer)
    elif bridge:
        offset = 6.0 * max(1, layer)
    else:
        offset = 0.0
    level = "underground" if (tunnel and tunnel_tag not in ("building_passage",)) else "elevated" if bridge else "surface"
    return {"tunnel": tunnel, "bridge": bridge, "osm_layer": layer, "level": level, "z_offset_m": offset}


def estimate_road(tags: dict, highway: str) -> dict:
    """Lanes/width/oneway/sidewalk/maxspeed for one road way: OSM when tagged,
    else estimated from class — and every estimate is flagged as such."""
    junction = tags.get("junction")
    oneway_tag = str(tags.get("oneway", "")).lower()
    reverse = oneway_tag == "-1" or oneway_tag == "reverse"
    oneway = oneway_tag in ("yes", "true", "1", "-1", "reverse") or junction in ("roundabout", "circular") or (highway in ("motorway", "motorway_link") and oneway_tag != "no")
    lanes_tag = parse_int(tags.get("lanes"))
    lanes_total = lanes_tag if lanes_tag else DEFAULT_LANES.get(highway, 1)
    lf = parse_int(tags.get("lanes:forward"))
    lb = parse_int(tags.get("lanes:backward"))
    if oneway:
        lanes_fwd, lanes_back = lanes_total, 0
    elif lf is not None and lb is not None:
        lanes_fwd, lanes_back = lf, lb
    else:
        lanes_fwd = max(1, (lanes_total + 1) // 2)
        lanes_back = max(1, lanes_total - lanes_fwd)
    width = parse_length_m(tags.get("width"))
    width_source = "osm" if width else "estimated"
    if not width:
        if highway in FOOT_CLASSES:
            width = MIN_WIDTH.get(highway, 2.0)
        else:
            width = max(MIN_WIDTH.get(highway.replace("_link", ""), 5.0), (lanes_fwd + lanes_back) * LANE_WIDTH)
    sw = str(tags.get("sidewalk", "")).lower()
    sw_l, sw_r = tags.get("sidewalk:left"), tags.get("sidewalk:right")
    sidewalk_source = "osm"
    if sw in ("both", "yes"):
        left, right = True, True
    elif sw == "left":
        left, right = True, False
    elif sw == "right":
        left, right = False, True
    elif sw in ("no", "none", "separate"):
        left = right = False
    elif sw_l or sw_r:
        left, right = sw_l in ("yes", "both"), sw_r in ("yes", "both")
    elif highway in SIDEWALK_DEFAULT and tags.get("highway") not in FOOT_CLASSES:
        left = right = True
        sidewalk_source = "estimated"
    else:
        left = right = False
        sidewalk_source = "estimated"
    speed = parse_speed_kmh(tags.get("maxspeed"))
    speed_source = "osm"
    if speed is None:
        speed = DEFAULT_SPEED.get(highway)
        speed_source = "estimated"
    return {
        "oneway": oneway, "reverse": reverse, "lanes": lanes_total, "lanes_forward": lanes_fwd, "lanes_backward": lanes_back,
        "width_m": r2(width), "width_source": width_source,
        "sidewalk": {"left": left, "right": right, "source": sidewalk_source},
        "maxspeed_kmh": speed, "maxspeed_source": speed_source,
    }


def way_points(way: dict, proj: Projection) -> tuple[list[tuple[float, float]], list[Optional[int]]]:
    pts, ids = [], []
    geometry = way.get("geometry") or []
    nodes = way.get("nodes") or [None] * len(geometry)
    for g, nid in zip(geometry, nodes):
        if not g:
            continue
        pts.append(proj.fwd(g["lat"], g["lon"]))
        ids.append(nid)
    return pts, ids


def polyline_length(pts: list) -> float:
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]) for i in range(len(pts) - 1))


def build_graph(ways: list[dict], node_info: dict, mode_of, id_prefix: str) -> dict:
    """Routing graph from a list of way records ({id, node_ids, pts, ys, mode, ...}).
    Junction = way endpoint, node shared by >= 2 ways, or a tagged crossing/signal.
    Edges are directed (two-way ways yield both directions) and keep their
    polyline (`pts`) so lanes/offsets can be derived. Nodes: {id: {x, z, y, lat, lon, ...}}."""
    usage: dict[int, int] = defaultdict(int)
    for w in ways:
        for nid in set(w["node_ids"]):
            if nid is not None:
                usage[nid] += 1
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    counter = 0

    def node_entry(nid, pt, y, level):
        key = str(nid)
        if key not in nodes:
            info = node_info.get(nid, {})
            nodes[key] = {"x": r2(pt[0]), "z": r2(pt[1]), "y": r2(y), "lat": info.get("lat"), "lon": info.get("lon"), "level": level}
            if info.get("signal"):
                nodes[key]["signal"] = True
            if info.get("crossing"):
                nodes[key]["crossing"] = True
        elif level == "underground":
            nodes[key]["level"] = "underground"
        return key

    for w in ways:
        ids, pts, ys = w["node_ids"], w["pts"], w["ys"]
        if len(pts) < 2:
            continue
        cuts = [0]
        for i in range(1, len(pts) - 1):
            nid = ids[i]
            info = node_info.get(nid, {})
            if nid is not None and (usage[nid] >= 2 or info.get("signal") or info.get("crossing")):
                cuts.append(i)
        cuts.append(len(pts) - 1)
        for a, b in zip(cuts, cuts[1:]):
            seg_pts, seg_ys = pts[a:b + 1], ys[a:b + 1]
            if w.get("reverse"):
                seg_from, seg_to = b, a
                seg_pts_f, seg_ys_f = seg_pts[::-1], seg_ys[::-1]
            else:
                seg_from, seg_to = a, b
                seg_pts_f, seg_ys_f = seg_pts, seg_ys
            if ids[a] is None or ids[b] is None:
                continue
            length = polyline_length(seg_pts)
            if length < 0.05:
                continue
            fa = node_entry(ids[seg_from], pts[seg_from], ys[seg_from], w["level"])
            fb = node_entry(ids[seg_to], pts[seg_to], ys[seg_to], w["level"])
            base = {
                "from": fa, "to": fb, "length_m": r2(length), "name": w.get("name"), "highway": w["highway"], "way": w["id"],
                "lanes": w.get("lanes_forward", 1), "width_m": w.get("width_m"), "maxspeed_kmh": w.get("maxspeed_kmh"),
                "oneway": w.get("oneway", False), "layer": w["level"], "osm_layer": w["osm_layer"], "tunnel": w["tunnel"], "bridge": w["bridge"],
                "mode": mode_of(w), "pts": [[r2(p[0]), r2(p[1])] for p in seg_pts_f],
            }
            if w.get("crossing"):
                base["crossing"] = True
            sw = w.get("sidewalk")
            if sw:
                # sides are relative to the way's own direction; an edge built against it (oneway=-1) swaps them
                flip = bool(w.get("reverse"))
                base["sidewalk_left"], base["sidewalk_right"] = (sw["right"], sw["left"]) if flip else (sw["left"], sw["right"])
            if w.get("transport_mode"):
                base["transport_mode"] = w["transport_mode"]
            counter += 1
            edges.append({**base, "id": f"{id_prefix}{counter}"})
            if not w.get("oneway"):
                rev = dict(base)
                if "sidewalk_left" in base:
                    rev["sidewalk_left"], rev["sidewalk_right"] = base["sidewalk_right"], base["sidewalk_left"]
                rev.update({"from": fb, "to": fa, "pts": base["pts"][::-1], "lanes": w.get("lanes_backward", w.get("lanes_forward", 1)), "id": f"{id_prefix}{counter}r"})
                edges.append(rev)
    return {"nodes": nodes, "edges": edges}


# --- the main entry point ---------------------------------------------------------------------

_RAIL_MODES = {"subway": "subway", "light_rail": "light_rail", "tram": "tram", "rail": "train", "narrow_gauge": "train", "monorail": "monorail", "funicular": "funicular"}
_RAIL_BAD = {"abandoned", "disused", "construction", "proposed", "razed", "preserved"}


def build_dataset(elements: list[dict], area: dict, terrain_grid: Optional[dict] = None, progress=None) -> dict:
    """`area`: {id, name, bbox{south,west,north,east}, origin{lat,lon}, source}.
    Returns the full schema-v1 dataset (JSON-serialisable)."""
    def tick(phase: str, done: int, total: int) -> None:
        if progress:
            progress(phase, done, total)

    origin = area["origin"]
    proj = Projection(origin["lat"], origin["lon"])
    terrain = Terrain(terrain_grid)
    ways = {e["id"]: e for e in elements if e["type"] == "way"}
    rels = [e for e in elements if e["type"] == "relation"]
    nodes_el = [e for e in elements if e["type"] == "node"]

    # per-node facts (signals, crossings, coordinates)
    node_info: dict[int, dict] = {}
    for n in nodes_el:
        tags = n.get("tags") or {}
        node_info[n["id"]] = {
            "lat": n["lat"], "lon": n["lon"],
            "signal": tags.get("highway") == "traffic_signals",
            "crossing": tags.get("highway") == "crossing" or tags.get("crossing") is not None and tags.get("highway") != "bus_stop",
        }
    for w in ways.values():
        for nid, g in zip(w.get("nodes") or [], w.get("geometry") or []):
            if g and nid not in node_info:
                node_info[nid] = {"lat": g["lat"], "lon": g["lon"]}

    tick("process", 0, 7)
    # ---------------- roads ----------------
    road_records: list[dict] = []
    road_graph_ways: list[dict] = []
    for w in ways.values():
        tags = w.get("tags") or {}
        highway = tags.get("highway")
        if not highway or highway in SKIP_HIGHWAY or tags.get("indoor") == "yes":
            continue
        if highway not in CAR_CLASSES and highway not in FOOT_CLASSES:
            continue
        if tags.get("area") == "yes":
            continue  # a pedestrian/plaza AREA: handled as ground
        pts, ids = way_points(w, proj)
        if len(pts) < 2:
            continue
        ys = [r2(terrain.elev(p[0], p[1])) for p in pts]
        level = way_level(tags, "road")
        est = estimate_road(tags, highway)
        is_car = highway in CAR_CLASSES
        crossing = tags.get("footway") == "crossing" or tags.get("highway") == "footway" and tags.get("footway") == "crossing"
        surface_tag = tags.get("surface")
        default_surface = ROAD_SURFACE_DEFAULT.get(highway.replace("_link", ""), "asphalt")
        road_slot = osm_slot(surface_tag, None, default_surface, SURFACE_COLOUR.get(default_surface))
        if road_slot["source"] == "osm" and not road_slot["colour"]:
            road_slot["colour"] = SURFACE_COLOUR.get(road_slot["material"])
        rid = f"w{w['id']}"
        record = {
            "id": rid, "osm_id": w["id"], "class": highway, "name": tags.get("name"), "ref": tags.get("ref"),
            "mode": "road" if is_car else "foot", **est, **level,
            "pts": [[r2(p[0]), r2(p[1])] for p in pts], "ys": ys, "node_ids": [str(i) if i else None for i in ids],
            "surfaces": {"road": {"surface_id": f"{rid}/road", **road_slot}},
        }
        if est["sidewalk"]["left"]:
            record["surfaces"]["sidewalk_left"] = {"surface_id": f"{rid}/sidewalk_left", **slot("paving_stones", "#b5b1a8", "default", 0.2)}
        if est["sidewalk"]["right"]:
            record["surfaces"]["sidewalk_right"] = {"surface_id": f"{rid}/sidewalk_right", **slot("paving_stones", "#b5b1a8", "default", 0.2)}
        if crossing:
            record["crossing"] = True
        if tags.get("cycleway") or tags.get("cycleway:left") or tags.get("cycleway:right") or highway == "cycleway":
            record["cycleway"] = True
        road_records.append(record)
        road_graph_ways.append({
            "id": rid, "node_ids": ids, "pts": pts, "ys": ys, "name": tags.get("name"), "highway": highway,
            "oneway": est["oneway"] and is_car, "reverse": est["reverse"], "lanes_forward": est["lanes_forward"] if is_car else 1,
            "lanes_backward": est["lanes_backward"] if is_car else 1, "width_m": est["width_m"], "maxspeed_kmh": est["maxspeed_kmh"],
            "level": level["level"], "osm_layer": level["osm_layer"], "tunnel": level["tunnel"], "bridge": level["bridge"], "crossing": crossing,
            "sidewalk": est["sidewalk"],
        })
    graph = build_graph(road_graph_ways, node_info, lambda w: "road" if w["highway"] in CAR_CLASSES else "foot", "e")
    tick("process", 1, 7)

    # ---------------- rail ----------------
    rail_ways: list[dict] = []
    rail_graph_ways: list[dict] = []
    platforms: list[dict] = []
    for w in ways.values():
        tags = w.get("tags") or {}
        rw = tags.get("railway")
        if rw == "platform" or tags.get("public_transport") == "platform":
            pts, _ids = way_points(w, proj)
            if len(pts) >= 2:
                centre = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                platforms.append({"id": f"w{w['id']}", "ref": tags.get("ref"), "name": tags.get("name"), "pos": [r2(centre[0]), r2(centre[1])],
                                  "pts": [[r2(p[0]), r2(p[1])] for p in pts], "mode": _RAIL_MODES.get(tags.get("railway_mode") or "", None),
                                  "level": way_level(tags, "rail")["level"], "surface_id": f"w{w['id']}/platform"})
            continue
        if rw not in _RAIL_MODES or _RAIL_BAD & {tags.get("railway:preserved"), tags.get("disused"), tags.get("abandoned")}:
            continue
        if tags.get("service") in ("yard", "siding") and rw in ("rail",) and tags.get("usage") not in ("main", "branch"):
            pass  # keep sidings: trains park there; flagged below
        pts, ids = way_points(w, proj)
        if len(pts) < 2:
            continue
        mode = _RAIL_MODES[rw]
        ys = [r2(terrain.elev(p[0], p[1])) for p in pts]
        level = way_level(tags, rw)
        rid = f"w{w['id']}"
        rail_ways.append({
            "id": rid, "osm_id": w["id"], "mode": mode, "railway": rw, "name": tags.get("name"), "ref": tags.get("ref"),
            "service": tags.get("service"), "usage": tags.get("usage"), "electrified": tags.get("electrified"), "gauge_mm": parse_int(tags.get("gauge")),
            "maxspeed_kmh": parse_speed_kmh(tags.get("maxspeed")), "oneway": tags.get("oneway") == "yes", **level,
            "pts": [[r2(p[0]), r2(p[1])] for p in pts], "ys": ys, "node_ids": [str(i) if i else None for i in ids],
            "surfaces": {"track": {"surface_id": f"{rid}/track", **slot("gravel" if level["level"] == "surface" else "concrete", "#6f6a62", "default", 0.2)}},
        })
        rail_graph_ways.append({
            "id": rid, "node_ids": ids, "pts": pts, "ys": ys, "name": tags.get("name") or tags.get("ref"), "highway": mode,
            "oneway": False, "lanes_forward": 1, "lanes_backward": 1, "width_m": 1.5, "maxspeed_kmh": parse_speed_kmh(tags.get("maxspeed")),
            "level": level["level"], "osm_layer": level["osm_layer"], "tunnel": level["tunnel"], "bridge": level["bridge"], "transport_mode": mode,
        })
    rail_graph = build_graph(rail_graph_ways, node_info, lambda w: w["transport_mode"], "t")
    tick("process", 2, 7)

    # stations, entrances, stop positions
    stations: list[dict] = []
    entrances: list[dict] = []
    stop_positions: dict[int, dict] = {}
    rail_index = GridIndex(100.0)
    for rw_rec in rail_ways:
        xs = [p[0] for p in rw_rec["pts"]]
        zs = [p[1] for p in rw_rec["pts"]]
        rail_index.add(rw_rec, (min(xs), min(zs), max(xs), max(zs)))

    def nearest_rail(x: float, z: float, radius: float = 30.0) -> Optional[dict]:
        best, best_d = None, radius
        for rw_rec in rail_index.at(x, z):
            for p in rw_rec["pts"]:
                d = math.hypot(p[0] - x, p[1] - z)
                if d < best_d:
                    best, best_d = rw_rec, d
        return best

    for n in nodes_el:
        tags = n.get("tags") or {}
        x, z = proj.fwd(n["lat"], n["lon"])
        railway = tags.get("railway")
        pt = tags.get("public_transport")
        if railway == "subway_entrance":
            entrances.append({"id": f"n{n['id']}", "name": tags.get("name"), "pos": [r2(x), r2(z)], "y": r2(terrain.elev(x, z)), "ref": tags.get("ref")})
        elif railway in ("station", "halt", "tram_stop", "stop") or (pt in ("station",) and tags.get("railway")):
            mode = "subway" if (tags.get("station") == "subway" or tags.get("subway") == "yes") else "tram" if (railway == "tram_stop" or tags.get("tram") == "yes") else \
                   "light_rail" if (tags.get("station") == "light_rail" or tags.get("light_rail") == "yes") else "train"
            near = nearest_rail(x, z)
            underground = tags.get("location") == "underground" or tags.get("tunnel") == "yes" or bool(near and near["tunnel"]) or mode == "subway"
            stations.append({
                "id": f"n{n['id']}", "osm_id": n["id"], "name": tags.get("name"), "mode": mode, "pos": [r2(x), r2(z)], "y": r2(terrain.elev(x, z)),
                "underground": underground, "z_offset_m": (near["z_offset_m"] if near else (-12.0 if underground else 0.0)),
                "ref": tags.get("ref"), "operator": tags.get("operator"), "network": tags.get("network"), "platform_ids": [], "entrance_ids": [],
            })
        elif pt == "stop_position":
            stop_positions[n["id"]] = {"pos": (x, z), "name": tags.get("name"), "mode": next((m for m in ("subway", "tram", "light_rail", "train", "bus") if tags.get(m) == "yes"), None)}
    # One physical station is usually several OSM nodes (one per line/platform
    # node): merge same-name, same-mode nodes within 150 m into one complex.
    merged: list[dict] = []
    for s in stations:
        for m in merged:
            if s["name"] and m["name"] == s["name"] and m["mode"] == s["mode"] and math.hypot(m["pos"][0] - s["pos"][0], m["pos"][1] - s["pos"][1]) < 150.0:
                m["osm_ids"].append(s["osm_id"])
                m["underground"] = m["underground"] or s["underground"]
                n = len(m["osm_ids"])
                m["pos"] = [r2((m["pos"][0] * (n - 1) + s["pos"][0]) / n), r2((m["pos"][1] * (n - 1) + s["pos"][1]) / n)]
                break
        else:
            s["osm_ids"] = [s["osm_id"]]
            merged.append(s)
    stations = merged
    station_by_osm = {oid: s for s in stations for oid in s["osm_ids"]}
    for p in platforms:
        best, best_d = None, 120.0
        for s in stations:
            d = math.hypot(s["pos"][0] - p["pos"][0], s["pos"][1] - p["pos"][1])
            if d < best_d:
                best, best_d = s, d
        if best:
            best["platform_ids"].append(p["id"])
            p["station_id"] = best["id"]
    for e in entrances:
        best, best_d = None, 160.0
        for s in stations:
            if s["mode"] not in ("subway", "light_rail", "train"):
                continue
            d = math.hypot(s["pos"][0] - e["pos"][0], s["pos"][1] - e["pos"][1])
            if d < best_d:
                best, best_d = s, d
        if best:
            best["entrance_ids"].append(e["id"])
            e["station_id"] = best["id"]
    for s in stations:
        s["surface_id"] = f"{s['id']}/station"
        s["surfaces"] = {"station": {"surface_id": f"{s['id']}/station", **slot("concrete", "#9c9a96", "default", 0.1)}}

    # route relations -> lines
    rail_by_id = {r["osm_id"]: r for r in rail_ways}
    lines: list[dict] = []
    for rel in rels:
        tags = rel.get("tags") or {}
        route = tags.get("route")
        if route not in ("subway", "tram", "light_rail", "train", "monorail"):
            continue
        member_ways = [m["ref"] for m in rel.get("members", []) if m["type"] == "way" and m.get("role", "") in ("", "forward", "backward", "route") and m["ref"] in rail_by_id]
        stop_refs = [m["ref"] for m in rel.get("members", []) if m["type"] == "node" and (m.get("role", "").startswith("stop") or m.get("role") == "platform")]
        if not member_ways:
            continue
        lines.append({
            "id": f"r{rel['id']}", "osm_id": rel["id"], "mode": "train" if route == "train" else route, "ref": tags.get("ref"), "name": tags.get("name"),
            "colour": parse_colour(tags.get("colour")) or None, "from": tags.get("from"), "to": tags.get("to"), "operator": tags.get("operator"),
            "network": tags.get("network"), "way_ids": [f"w{i}" for i in member_ways], "_stop_refs": stop_refs,
        })
    # chain each line's ways into continuous paths and place its stops along them
    rail_pts_meta = {r["id"]: r for r in rail_ways}
    for line in lines:
        paths: list[list[list[float]]] = []
        metas: list[list[tuple[float, float]]] = []  # per path vertex: (terrain y, z_offset_m)
        current: list[list[float]] = []
        current_meta: list[tuple[float, float]] = []
        for wid in line["way_ids"]:
            rw_rec = rail_pts_meta[wid]
            pts = [list(p) for p in rw_rec["pts"]]
            meta = [(y, rw_rec["z_offset_m"]) for y in rw_rec["ys"]]
            if not current:
                current, current_meta = pts, meta
                continue
            tail = current[-1]
            if math.hypot(pts[0][0] - tail[0], pts[0][1] - tail[1]) < 1.0:
                current.extend(pts[1:])
                current_meta.extend(meta[1:])
            elif math.hypot(pts[-1][0] - tail[0], pts[-1][1] - tail[1]) < 1.0:
                current.extend(pts[::-1][1:])
                current_meta.extend(meta[::-1][1:])
            elif math.hypot(pts[0][0] - current[0][0], pts[0][1] - current[0][1]) < 1.0:
                current = pts[::-1][:-1] + current
                current_meta = meta[::-1][:-1] + current_meta
            elif math.hypot(pts[-1][0] - current[0][0], pts[-1][1] - current[0][1]) < 1.0:
                current = pts[:-1] + current
                current_meta = meta[:-1] + current_meta
            else:
                paths.append(current)
                metas.append(current_meta)
                current, current_meta = pts, meta
        if current:
            paths.append(current)
            metas.append(current_meta)
        order = sorted(range(len(paths)), key=lambda i: polyline_length(paths[i]), reverse=True)
        paths = [paths[i] for i in order]
        metas = [metas[i] for i in order]
        line["paths_y"] = [[m[0] for m in meta] for meta in metas]
        line["paths_z_offset_m"] = [[m[1] for m in meta] for meta in metas]
        line["paths"] = paths
        line_stops = []
        main = paths[0] if paths else []
        for ref in line.pop("_stop_refs"):
            st = station_by_osm.get(ref)
            sp = stop_positions.get(ref)
            if st is None and sp is None:
                continue
            px, pz = (st["pos"] if st else sp["pos"])
            if not main:
                continue
            # project onto the main path: arclength of the nearest vertex
            best_i, best_d, acc, s_at = 0, 1e18, 0.0, 0.0
            for i, p in enumerate(main):
                if i:
                    acc += math.hypot(p[0] - main[i - 1][0], p[1] - main[i - 1][1])
                d = math.hypot(p[0] - px, p[1] - pz)
                if d < best_d:
                    best_d, best_i, s_at = d, i, acc
            if best_d <= 60.0:
                line_stops.append({"station_id": st["id"] if st else None, "name": (st or sp)["name"], "pos": [r2(px), r2(pz)], "s_m": r2(s_at)})
        line["stops"] = line_stops
    tick("process", 3, 7)

    # ---------------- buildings ----------------
    buildings: list[dict] = []
    footprint_rings: list[tuple[str, list, list]] = []  # (id, outer, holes)
    candidates: list[tuple[str, dict, list, list]] = []
    for w in ways.values():
        tags = w.get("tags") or {}
        if not (tags.get("building") or tags.get("building:part")) or tags.get("building") == "no":
            continue
        pts, _ids = way_points(w, proj)
        if len(pts) >= 4 and pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) < 3:
            continue
        candidates.append((f"w{w['id']}", tags, pts, []))
    for rel in rels:
        tags = rel.get("tags") or {}
        if not (tags.get("building") or tags.get("building:part")) or tags.get("type") != "multipolygon":
            continue
        outers = assemble_rings([[(g["lat"], g["lon"]) for g in m["geometry"] if g] for m in rel.get("members", []) if m.get("role") == "outer" and m.get("geometry")])
        inners = assemble_rings([[(g["lat"], g["lon"]) for g in m["geometry"] if g] for m in rel.get("members", []) if m.get("role") == "inner" and m.get("geometry")])
        outer_xz = [[proj.fwd(*p) for p in ring] for ring in outers]
        inner_xz = [[proj.fwd(*p) for p in ring] for ring in inners]
        for oi, outer in enumerate(outer_xz):
            holes = [h for h in inner_xz if point_in_ring(*ring_centroid(h), outer)]
            candidates.append((f"r{rel['id']}" + (f".{oi}" if len(outer_xz) > 1 else ""), tags, outer, holes))
    for bid, tags, outer, holes in candidates:
        outer = orient(outer, True)
        holes = [orient(h, False) for h in holes]
        cx, cz = ring_centroid(outer)
        base_y = r2(min(terrain.elev(p[0], p[1]) for p in outer[::max(1, len(outer) // 6)]))
        height = parse_length_m(tags.get("height"))
        levels = parse_int(tags.get("building:levels"))
        roof_levels = parse_int(tags.get("roof:levels"))
        roof_h = parse_length_m(tags.get("roof:height"))
        if height:
            h_source = "tag"
        elif levels:
            height = levels * 3.0 + (roof_h if roof_h else 0.0)
            h_source = "levels"
        else:
            height = {"house": 7.0, "detached": 7.0, "semidetached_house": 7.0, "garage": 3.0, "garages": 3.0, "shed": 2.5, "hut": 2.5, "roof": 4.0, "carport": 3.0,
                      "apartments": 15.0, "commercial": 12.0, "retail": 8.0, "industrial": 9.0, "warehouse": 8.0, "church": 18.0, "cathedral": 30.0}.get(tags.get("building", ""), 9.0)
            h_source = "estimated_default"
        min_h = parse_length_m(tags.get("min_height")) or ((parse_int(tags.get("building:min_level")) or 0) * 3.0) or 0.0
        raw_type = tags.get("building") if tags.get("building") not in (None, "yes") else (tags.get("building:part") or "yes")
        designation = _designate(raw_type, tags)
        wall = osm_slot(tags.get("building:material") or tags.get("building:facade:material"), tags.get("building:colour") or tags.get("colour"))
        roof = osm_slot(tags.get("roof:material"), tags.get("roof:colour"))
        is_part = bool(tags.get("building:part")) and not tags.get("building")
        record = {
            "id": bid, "kind": "part" if is_part else "building", "type_raw": raw_type, "type": designation, "name": tags.get("name"),
            "address": _address(tags), "footprint": {"outer": [[r2(p[0]), r2(p[1])] for p in outer], "holes": [[[r2(p[0]), r2(p[1])] for p in h] for h in holes]},
            "centroid": [r2(cx), r2(cz)], "area_m2": r2(abs(ring_area(outer)) - sum(abs(ring_area(h)) for h in holes)),
            "base_y": base_y, "height_m": r2(height), "height_source": h_source, "levels": levels, "min_height_m": r2(min_h),
            "roof": {"shape": tags.get("roof:shape"), "height_m": roof_h, "levels": roof_levels, "direction": parse_int(tags.get("roof:direction")),
                     "orientation": tags.get("roof:orientation")},
            "parts": [], "part_of": None, "has_parts": False,
            "surfaces": {"wall": {"surface_id": f"{bid}/wall", **wall}, "roof": {"surface_id": f"{bid}/roof", **roof}},
        }
        buildings.append(record)
        footprint_rings.append((bid, outer, holes))
    # parts <-> parents
    b_index = GridIndex(80.0)
    by_id = {b["id"]: b for b in buildings}
    ring_of = {bid: (outer, holes) for bid, outer, holes in footprint_rings}
    for b in buildings:
        if b["kind"] == "building":
            b_index.add(b["id"], ring_bbox(ring_of[b["id"]][0]))
    for b in buildings:
        if b["kind"] != "part":
            continue
        cx, cz = b["centroid"]
        for pid in b_index.at(cx, cz):
            if point_in_ring(cx, cz, ring_of[pid][0]):
                b["part_of"] = pid
                by_id[pid]["parts"].append(b["id"])
                by_id[pid]["has_parts"] = True
                break
    tick("process", 4, 7)

    # ---------------- ground ----------------
    ground_areas: list[dict] = []
    ground_lines: list[dict] = []
    trees: list[list[float]] = []
    g_candidates: list[tuple[str, dict, list, list]] = []
    for w in ways.values():
        tags = w.get("tags") or {}
        if tags.get("building") or tags.get("building:part"):
            continue
        cls = _ground_class(tags)
        if tags.get("waterway") and not tags.get("area") and tags.get("waterway") in ("river", "stream", "canal", "drain", "ditch"):
            pts, _ids = way_points(w, proj)
            if len(pts) >= 2:
                wid = f"w{w['id']}"
                ground_lines.append({"id": wid, "class": tags["waterway"], "name": tags.get("name"), "width_m": parse_length_m(tags.get("width")) or {"river": 12.0, "canal": 8.0, "stream": 2.0}.get(tags["waterway"], 1.5),
                                     "tunnel": bool(tags.get("tunnel")) and tags.get("tunnel") != "no", "pts": [[r2(p[0]), r2(p[1])] for p in pts],
                                     "ys": [r2(terrain.elev(p[0], p[1])) for p in pts], "surface_id": f"{wid}/water", "surface": slot("water", "#3f78a8", "default", 0.3)})
            continue
        if cls is None:
            continue
        if tags.get("highway") in CAR_CLASSES and tags.get("area") != "yes":
            continue
        pts, _ids = way_points(w, proj)
        if len(pts) >= 4 and pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) >= 3:
            g_candidates.append((f"w{w['id']}", tags, pts, []))
    for rel in rels:
        tags = rel.get("tags") or {}
        if tags.get("type") != "multipolygon" or tags.get("building") or _ground_class(tags) is None:
            continue
        outers = assemble_rings([[(g["lat"], g["lon"]) for g in m["geometry"] if g] for m in rel.get("members", []) if m.get("role") == "outer" and m.get("geometry")])
        inners = assemble_rings([[(g["lat"], g["lon"]) for g in m["geometry"] if g] for m in rel.get("members", []) if m.get("role") == "inner" and m.get("geometry")])
        outer_xz = [[proj.fwd(*p) for p in ring] for ring in outers]
        inner_xz = [[proj.fwd(*p) for p in ring] for ring in inners]
        for oi, outer in enumerate(outer_xz):
            holes = [h for h in inner_xz if point_in_ring(*ring_centroid(h), outer)]
            g_candidates.append((f"r{rel['id']}" + (f".{oi}" if len(outer_xz) > 1 else ""), tags, outer, holes))
    for gid, tags, outer, holes in g_candidates:
        cls = _ground_class(tags)
        if cls is None:
            continue
        outer = orient(outer, True)
        ground_areas.append({
            "id": f"g{gid}", "class": cls, "name": tags.get("name"), "raw": {k: tags[k] for k in ("landuse", "natural", "leisure", "amenity", "water", "place", "highway") if k in tags},
            "outer": [[r2(p[0]), r2(p[1])] for p in outer], "holes": [[[r2(p[0]), r2(p[1])] for p in orient(h, False)] for h in holes],
            "area_m2": r2(abs(ring_area(outer))), "z_order": _GROUND_Z.get(cls, 1), "surface_id": f"g{gid}/ground", "surface": _ground_slot(cls),
        })
    ground_areas.sort(key=lambda a: a["z_order"])
    for n in nodes_el:
        tags = n.get("tags") or {}
        if tags.get("natural") == "tree":
            x, z = proj.fwd(n["lat"], n["lon"])
            trees.append([r2(x), r2(z), parse_length_m(tags.get("height")) or 0.0])
    tick("process", 5, 7)

    # ---------------- POIs ----------------
    pois: list[dict] = []
    for n in nodes_el:
        tags = n.get("tags") or {}
        kind, sub = _poi_kind(tags)
        if kind is None:
            continue
        x, z = proj.fwd(n["lat"], n["lon"])
        pois.append({"id": f"n{n['id']}", "kind": kind, "subkind": sub, "name": tags.get("name"), "pos": [r2(x), r2(z)], "y": r2(terrain.elev(x, z)),
                     "opening_hours": tags.get("opening_hours"), "level": tags.get("level")})
    for b_tags_id, tags in ((f"w{w['id']}", w.get("tags") or {}) for w in ways.values()):
        kind, sub = _poi_kind(tags)
        if kind is None or b_tags_id not in by_id:
            continue
        b = by_id[b_tags_id]
        pois.append({"id": b_tags_id, "kind": kind, "subkind": sub, "name": tags.get("name"), "pos": b["centroid"], "y": b["base_y"],
                     "opening_hours": tags.get("opening_hours"), "level": tags.get("level"), "building_id": b_tags_id})
    for p in pois:
        if "building_id" in p:
            continue
        for bid in b_index.at(p["pos"][0], p["pos"][1]):
            if point_in_ring(p["pos"][0], p["pos"][1], ring_of[bid][0]):
                p["building_id"] = bid
                break
    tick("process", 6, 7)

    bbox = area["bbox"]
    x0, z0 = proj.fwd(bbox["south"], bbox["west"])
    x1, z1 = proj.fwd(bbox["north"], bbox["east"])
    dataset = {
        "schema_version": SCHEMA_VERSION,
        "area": {
            "id": area["id"], "name": area.get("name") or area["id"], "origin": {"lat": origin["lat"], "lon": origin["lon"], "elev_m": (terrain_grid or {}).get("base_elev_m", 0.0)},
            "bbox": bbox, "extent_m": {"x0": r2(x0), "z0": r2(z0), "x1": r2(x1), "z1": r2(z1)}, "source": area.get("source", "osm"),
            "osm_timestamp": area.get("osm_timestamp"), "generated_at": area.get("generated_at"),
            "attribution": ATTRIBUTION, "license": LICENSE,
            "coordinate_note": "x = east, z = north, y = up; metres in a local tangent plane around origin; y=0 is terrain at the origin",
        },
        "roads": road_records,
        "graph": graph,
        "rail": {"ways": rail_ways, "graph": rail_graph, "stations": stations, "platforms": platforms, "entrances": entrances, "lines": lines},
        "buildings": buildings,
        "ground": {"base": {"surface_id": "ground/base", "class": "soil", "surface": slot("grass", "#6b8f47", "default", 0.2)}, "areas": ground_areas, "lines": ground_lines, "trees": trees},
        "terrain": terrain_grid,
        "pois": pois,
    }
    dataset["counts"] = {
        "roads": len(road_records), "road_edges": len(graph["edges"]), "road_nodes": len(graph["nodes"]),
        "rail_ways": len(rail_ways), "rail_lines": len(lines), "stations": len(stations), "platforms": len(platforms), "entrances": len(entrances),
        "buildings": sum(1 for b in buildings if b["kind"] == "building"), "building_parts": sum(1 for b in buildings if b["kind"] == "part"),
        "ground_areas": len(ground_areas), "water_lines": len(ground_lines), "trees": len(trees), "pois": len(pois),
        "underground_roads": sum(1 for r in road_records if r["level"] == "underground"), "underground_rail": sum(1 for r in rail_ways if r["level"] == "underground"),
    }
    tick("process", 7, 7)
    return dataset


# --- classification helpers ----------------------------------------------------------------------

_BUILDING_DESIGNATION = {
    "residential": "residential", "apartments": "residential", "house": "residential", "detached": "residential", "semidetached_house": "residential",
    "terrace": "residential", "dormitory": "residential", "bungalow": "residential", "farm": "residential",
    "retail": "shop", "commercial": "shop", "supermarket": "shop", "kiosk": "shop", "shop": "shop",
    "office": "office", "hotel": "office", "restaurant": "restaurant", "cafe": "restaurant",
    "warehouse": "warehouse", "industrial": "industrial", "factory": "industrial",
    "school": "civic", "university": "civic", "hospital": "civic", "church": "civic", "cathedral": "civic", "chapel": "civic", "public": "civic",
    "civic": "civic", "government": "civic", "kindergarten": "civic", "train_station": "civic", "transportation": "civic", "fire_station": "civic",
}


def _designate(raw: str, tags: dict) -> str:
    if raw in _BUILDING_DESIGNATION:
        return _BUILDING_DESIGNATION[raw]
    if tags.get("shop"):
        return "shop"
    if tags.get("office"):
        return "office"
    if tags.get("amenity") in ("restaurant", "cafe", "fast_food", "pub", "bar"):
        return "restaurant"
    if tags.get("amenity") in ("school", "university", "hospital", "townhall", "place_of_worship", "kindergarten"):
        return "civic"
    return "other"


def _address(tags: dict) -> Optional[str]:
    street, number = tags.get("addr:street"), tags.get("addr:housenumber")
    return f"{street} {number}".strip() if street and number else (street or None)


_GROUND_Z = {"water": 5, "wetland": 4, "plaza": 3, "parking": 3, "sports": 3, "park": 2, "forest": 2, "grass": 2, "farmland": 2, "residential_area": 1, "industrial": 1, "commercial": 1, "paved": 2}


def _ground_class(tags: dict) -> Optional[str]:
    landuse, natural, leisure = tags.get("landuse"), tags.get("natural"), tags.get("leisure")
    if natural == "water" or tags.get("waterway") == "riverbank" or landuse in ("reservoir", "basin"):
        return "water"
    if natural == "wetland":
        return "wetland"
    if natural in ("wood", "scrub", "heath") or landuse == "forest":
        return "forest"
    if natural in ("grassland", "fell") or landuse in ("grass", "meadow", "village_green", "recreation_ground", "greenfield"):
        return "grass"
    if leisure in ("park", "garden", "nature_reserve", "common", "dog_park", "playground"):
        return "park"
    if natural in ("beach", "sand"):
        return "sand"
    if natural in ("bare_rock", "scree", "rock"):
        return "rock"
    if leisure in ("pitch", "sports_centre", "stadium", "track", "golf_course"):
        return "sports"
    if landuse in ("farmland", "orchard", "vineyard", "farmyard", "allotments", "plant_nursery"):
        return "farmland"
    if tags.get("amenity") == "parking" or landuse == "garages":
        return "parking"
    if tags.get("place") == "square" or (tags.get("highway") in ("pedestrian", "footway") and tags.get("area") == "yes"):
        return "plaza"
    if landuse == "cemetery":
        return "park"
    if landuse in ("industrial", "railway", "quarry", "brownfield", "construction", "landfill"):
        return "industrial"
    if landuse in ("commercial", "retail"):
        return "commercial"
    if landuse in ("residential", "education", "institutional", "civic_admin", "religious"):
        return "residential_area"
    return None


_GROUND_SLOT = {
    "water": ("water", "#3f78a8"), "wetland": ("wetland", "#5b8a6a"), "forest": ("grass", "#3f6b2e"), "grass": ("grass", "#6f9a47"),
    "park": ("grass", "#6aa04a"), "sand": ("sand", "#d8c9a0"), "rock": ("stone", "#8d8a85"), "sports": ("grass", "#5fa14d"),
    "farmland": ("dirt", "#9c8a5a"), "parking": ("asphalt", "#55565a"), "plaza": ("paving_stones", "#a8a398"), "industrial": ("concrete", "#8f8d89"),
    "commercial": ("paving_stones", "#a09c94"), "residential_area": ("grass", "#86a05f"),
}


def _ground_slot(cls: str) -> dict:
    material, colour = _GROUND_SLOT.get(cls, ("grass", "#7a9a55"))
    return slot(material, colour, "default", 0.3)


def _poi_kind(tags: dict) -> tuple[Optional[str], Optional[str]]:
    amenity, shop, office, tourism, leisure = tags.get("amenity"), tags.get("shop"), tags.get("office"), tags.get("tourism"), tags.get("leisure")
    if shop:
        return "shop", shop
    if amenity in ("restaurant", "fast_food", "cafe", "pub", "bar", "biergarten", "food_court", "ice_cream", "bistro"):
        return "food", amenity
    if amenity in ("school", "kindergarten", "university", "college", "language_school", "music_school", "driving_school"):
        return "education", amenity
    if amenity in ("hospital", "clinic", "doctors", "dentist", "pharmacy", "veterinary"):
        return "health", amenity
    if amenity in ("bank", "post_office", "post_box", "atm", "townhall", "police", "fire_station", "courthouse", "library", "community_centre"):
        return "service", amenity
    if amenity in ("theatre", "cinema", "arts_centre", "nightclub", "place_of_worship", "museum"):
        return "culture", amenity
    if amenity in ("fuel", "charging_station", "bicycle_rental", "car_sharing", "car_rental"):
        return "mobility", amenity
    if office:
        return "work", office
    if tourism in ("hotel", "hostel", "guest_house", "apartment", "motel"):
        return "lodging", tourism
    if tourism in ("museum", "gallery", "attraction", "viewpoint", "artwork", "zoo"):
        return "culture", tourism
    if leisure in ("playground", "pitch", "sports_centre", "fitness_centre", "swimming_pool", "dog_park"):
        return "leisure", leisure
    if tags.get("highway") == "bus_stop" or tags.get("public_transport") in ("stop_position", "platform") and tags.get("bus") == "yes":
        return "transit", "bus_stop"
    if tags.get("railway") in ("tram_stop", "halt", "station", "stop"):
        return "transit", tags["railway"]
    return None, None
