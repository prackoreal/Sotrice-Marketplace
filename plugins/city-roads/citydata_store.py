"""Shared access to the on-disk city datasets (the cache osm_fetch fills and
city_data.py publishes), for every city plugin: which area is active, load
its dataset, and adapt it to the LEGACY attribute shapes the older plugins
and renderers still consume (`road_network`, one entity per building).

Layout under osm_fetch.cache_root():
    active_area.json              {area, agent_count, vehicle_count, ...}
    <area_id>/dataset.json.gz     schema-v1 dataset (docs/city-schema/schema.md)
    <area_id>/raw/, terrain/      download caches
    <area_id>/surfaces.json       surface-override patches (looks / user)
    <area_id>/overlay_*.json      sandbox edits (moved/edited buildings, regenerated roads)
    geocode/                      Nominatim answers
"""

import json
import os
import re
from typing import Optional

import osm_fetch

_HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY_DIR = os.path.join(_HERE, "city_roads_data")
LEGACY_ACTIVE = os.path.join(LEGACY_DIR, "active_area.json")
DEFAULT_AREA = "klosterneuburg_center"
DEFAULT_SIZE_M = 1200


def area_dir(area_id: str) -> str:
    return os.path.join(osm_fetch.cache_root(), area_id)


def dataset_path(area_id: str) -> str:
    return os.path.join(area_dir(area_id), "dataset.json.gz")


def has_dataset(area_id: str) -> bool:
    return os.path.exists(dataset_path(area_id))


def load_dataset(area_id: str) -> Optional[dict]:
    path = dataset_path(area_id)
    if not os.path.exists(path):
        return None
    return osm_fetch.read_json_gz(path)


def slugify(name: str, size_m: Optional[int] = None) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower().split(",")[0] if "," in name and len(name.split(",")[0]) > 3 else name.lower()).strip("_")
    base = base[:48] or "area"
    return f"{base}_{int(size_m)}m" if size_m else base


# --- active-area configuration -----------------------------------------------------

def _active_path() -> str:
    return os.path.join(osm_fetch.cache_root(), "active_area.json")


def read_active() -> dict:
    """The active area + agent counts. The user-level file wins; the repo's
    own active_area.json (the pre-existing mechanism) is the fallback, so
    nothing configured before this existed stops working."""
    for path in (_active_path(), LEGACY_ACTIVE):
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
                if isinstance(cfg, dict):
                    cfg.setdefault("area", DEFAULT_AREA)
                    return cfg
            except (OSError, ValueError):
                continue
    return {"area": DEFAULT_AREA, "agent_count": 8, "vehicle_count": 3}


def write_active(update: dict) -> dict:
    cfg = read_active()
    cfg.update({k: v for k, v in update.items() if v is not None})
    cfg.pop("_comment", None)
    tmp = _active_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, _active_path())
    return cfg


def list_cached_areas() -> list[dict]:
    out = []
    root = osm_fetch.cache_root()
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, "dataset.json.gz")
        if os.path.exists(path):
            try:
                ds = osm_fetch.read_json_gz(path)
            except Exception:
                continue
            out.append({"id": name, "name": ds["area"]["name"], "counts": ds.get("counts", {}), "generated_at": ds["area"].get("generated_at"), "size_kb": os.path.getsize(path) // 1024})
    return out


# --- per-area sandbox overlay files (edits made by the user) -------------------------------

def overlay_path(area_id: str, kind: str) -> str:
    return os.path.join(area_dir(area_id), f"overlay_{kind}.json")


def load_overlay(area_id: str, kind: str, legacy_path: Optional[str] = None, default=None):
    for path in (overlay_path(area_id, kind), legacy_path):
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return default


def save_overlay(area_id: str, kind: str, value) -> None:
    path = overlay_path(area_id, kind)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
    os.replace(tmp, path)


# --- legacy adapters ------------------------------------------------------------------------

def legacy_road_network(ds: dict) -> dict:
    """Today's `road_network` shape from a dataset graph: nodes keyed by OSM id
    with x/z/lat/lon (+ y, layer), edges {from, to, length_m, name, highway}
    plus the new scalar fields, WITHOUT the polylines (those travel in the
    tile entities); rail edges are included so transit-sim keeps finding
    its lines (highway = rail mode, transport_mode, layer)."""
    nodes: dict = {}
    for source in (ds["graph"], ds["rail"]["graph"]):
        for nid, n in source["nodes"].items():
            nodes[nid] = {"x": n["x"], "z": n["z"], "y": n.get("y", 0.0), "lat": n.get("lat"), "lon": n.get("lon"), "layer": n.get("level", "surface")}
    edges = []
    for source in (ds["graph"], ds["rail"]["graph"]):
        for e in source["edges"]:
            edge = {k: e[k] for k in ("from", "to", "length_m", "name", "highway", "id", "way", "lanes", "width_m", "maxspeed_kmh", "oneway", "layer", "tunnel", "bridge", "mode", "crossing", "transport_mode") if k in e and e[k] is not None}
            edges.append(edge)
    return {"nodes": nodes, "edges": edges}


def legacy_buildings(ds: dict) -> list[dict]:
    """Today's building dicts (what city_buildings.py iterates) from a dataset.
    `id` is the bare OSM way id for ways (the key sandbox overlays were stored
    under), `r<id>` for relations; parts of a split building are skipped when
    the outline is kept and vice versa: parts replace their outline."""
    out = []
    for b in ds["buildings"]:
        if b["has_parts"]:
            continue  # rendered through its parts
        legacy_id = b["id"][1:] if b["id"].startswith("w") else b["id"]
        outer = b["footprint"]["outer"]
        wall = b["surfaces"]["wall"]
        out.append({
            "id": legacy_id, "dataset_id": b["id"], "footprint": [{"x": p[0], "z": p[1]} for p in outer], "holes": b["footprint"]["holes"],
            "position": {"x": b["centroid"][0], "z": b["centroid"][1]}, "height_m": b["height_m"],
            "height_source": b["height_source"], "building_type": b["type"], "type_raw": b["type_raw"], "name": b["name"],
            "wall_material": wall["material"] if wall["source"] != "default" else None, "wall_colour": wall["colour"],
            "levels": b["levels"], "min_height_m": b["min_height_m"], "base_y": b["base_y"], "roof": b["roof"], "part_of": b["part_of"],
            "surfaces": b["surfaces"],
        })
    return out
