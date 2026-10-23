"""city-data — the plugin that owns WHICH place the city shows and publishes
its real OpenStreetMap data to Core (schema v1: docs/city-schema/schema.md).

* Area selection, live: any place name (Nominatim), a bbox, a lat/lon centre,
  or an already-cached area id, via the `city_area_request` attribute on this
  plugin's control entity. No restart, no UI freeze: the work runs on its own
  worker thread and reports `city_progress`.
* Download once, cache forever (osm_fetch / citydata_store): the second time
  an area is instant and works offline.
* Publishes the dataset as a control entity (`city_dataset`, `city_terrain`,
  `city_rail`, `city_road_graph`, `city_surfaces`, ...) plus one entity per
  500 m tile (`ct_roads`, `ct_buildings`, `ct_ground`, `ct_pois`,
  `ct_rail_ways`) so a renderer can stream tiles in. Core has no entity
  deletion, so tile entities are POOLED and reused across area changes;
  unused ones are published as retired (`ct_tile.retired = true`).
* `city_surface_patch` -> persisted override slots (`city_surfaces`), never
  edits of the dataset itself.
* Looks (docs/city-schema/looks.md): `ct_looks` per tile (published right after
  `ct_tile`, before the tile's other attributes), `city_looks_meta` on the
  control entity, `city_looks_patch` -> looks_overrides.json -> `city_looks_ack`
  and a republish of only the touched tiles' `ct_looks`.

The legacy plugins (city-roads -> `road_network`, city-buildings -> building
entities) read the very same cached dataset and follow `city_dataset`.

Registered in plugins/registry.json as "city-data".
"""

import base64
import json
import math
import os
import struct
import threading
import time
from typing import Optional

import city_appearance as looks_lib
import citydata_store as store
import osm_city
import osm_fetch
from sotrice_client import World

TILE_M = 500.0
DEFAULT_SIZE_M = store.DEFAULT_SIZE_M
MIN_SIZE_M, MAX_SIZE_M = 200, 4000

# Curated starting points for the area picker (the same bboxes the example datasets were built from).
CURATED_AREAS = [
    {"id": "vienna_stephansplatz_karlsplatz", "name": "Vienna: Stephansplatz to Karlsplatz", "description": "Dense old town: U1/U2/U4 underground, trams, 1600+ buildings",
     "bbox": {"south": 48.1985, "west": 16.3600, "north": 48.2125, "east": 16.3850}},
    {"id": "klosterneuburg_center", "name": "Klosterneuburg centre", "description": "Hilly town centre with the S-Bahn line",
     "bbox": {"south": 48.3000, "west": 16.3195, "north": 48.3108, "east": 16.3355}},
]


def parse_coordinates(text: str) -> Optional[dict]:
    """'south,west,north,east' or 'lat,lon' -> {"bbox"|"center"}; None otherwise."""
    parts = [p.strip() for p in text.replace(";", ",").split(",")]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 4:
        s, w, n, e = nums
        if -90 <= s < n <= 90 and -180 <= w < e <= 180:
            return {"bbox": {"south": s, "west": w, "north": n, "east": e}}
    if len(nums) == 2 and -90 <= nums[0] <= 90 and -180 <= nums[1] <= 180:
        return {"center": {"lat": nums[0], "lon": nums[1]}}
    return None


def bbox_around(lat: float, lon: float, size_m: float) -> dict:
    half = size_m / 2.0
    dlat = half / 111195.0
    dlon = half / (111195.0 * math.cos(math.radians(lat)))
    return {"south": lat - dlat, "north": lat + dlat, "west": lon - dlon, "east": lon + dlon}


def resolve_request(request: dict, progress) -> dict:
    """request {query?, bbox?, center?, size_m?, area_id?} -> {id, name, bbox, origin}.
    Raises ValueError with a human message for anything unresolvable."""
    size_m = int(max(MIN_SIZE_M, min(MAX_SIZE_M, float(request.get("size_m") or DEFAULT_SIZE_M))))
    area_id = request.get("area_id")
    if area_id and store.has_dataset(area_id):
        ds = store.load_dataset(area_id)
        return {"id": area_id, "name": ds["area"]["name"], "bbox": ds["area"]["bbox"], "origin": {"lat": ds["area"]["origin"]["lat"], "lon": ds["area"]["origin"]["lon"]}}
    name = request.get("name")
    bbox = request.get("bbox")
    center = request.get("center")
    query = (request.get("query") or "").strip()
    if not (bbox or center) and query:
        coords = parse_coordinates(query)
        if coords:
            bbox, center = coords.get("bbox"), coords.get("center")
        else:
            progress("geocode", 0, 1, f"Searching for {query!r}")
            hits = osm_fetch.geocode(query)
            if not hits:
                raise ValueError(f"No place found for {query!r}")
            best = hits[0]
            center = {"lat": best["lat"], "lon": best["lon"]}
            name = best["name"]
            progress("geocode", 1, 1, f"Found {name}")
    if bbox is None and center is None:
        raise ValueError("Give a place name, a lat,lon centre or a south,west,north,east box")
    if bbox is None:
        bbox = bbox_around(center["lat"], center["lon"], size_m)
    origin = {"lat": (bbox["south"] + bbox["north"]) / 2.0, "lon": (bbox["west"] + bbox["east"]) / 2.0}
    name = name or query or f"{origin['lat']:.4f}, {origin['lon']:.4f}"
    return {"id": area_id or store.slugify(query or name, size_m), "name": name, "bbox": bbox, "origin": origin}


def build_area(area: dict, progress, force: bool = False) -> tuple[dict, dict]:
    """Cache hit -> read; else Overpass + terrain + processing, then cache.
    Returns (dataset, {"hit": bool, "seconds": float})."""
    started = time.time()
    if store.has_dataset(area["id"]) and not force:
        progress("process", 0, 1, "Loading from cache")
        ds = store.load_dataset(area["id"])
        if ds is not None and ds.get("generator_rev", 1) >= osm_city.GENERATOR_REV:
            return ds, {"hit": True, "seconds": round(time.time() - started, 2)}
        # older converter output: rebuild below from the cached OSM download (no new download unless the cache is gone)
    directory = store.area_dir(area["id"])
    elements, info = osm_fetch.fetch_features(directory, area["bbox"], lambda p, a, b, m=None: progress(p, a, b, m), force=force)
    proj = osm_city.Projection(area["origin"]["lat"], area["origin"]["lon"])
    terrain = osm_fetch.fetch_terrain(directory, area["bbox"], 10.0, proj.fwd, proj.inv, lambda p, a, b, m=None: progress(p, a, b, m))
    dataset = osm_city.build_dataset(
        elements,
        {**area, "source": "osm", "osm_timestamp": info["osm_timestamp"], "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        terrain,
        lambda p, a, b, m=None: progress(p, a, b, m),
    )
    osm_fetch.write_json_gz(store.dataset_path(area["id"]), dataset)
    return dataset, {"hit": False, "seconds": round(time.time() - started, 2)}


def apply_building_overlay(ds: dict, overlay: dict) -> int:
    """Applies the sandbox/agent edits of city-buildings (overlay_buildings.json, keyed by the legacy building
    id = OSM way id without the leading "w") to the dataset's buildings IN MEMORY (the cached dataset on disk is
    never touched): height, name, type, wall material, and a moved footprint. Returns how many were applied."""
    if not overlay:
        return 0
    by_legacy = {(b["id"][1:] if b["id"].startswith("w") else b["id"]): b for b in ds["buildings"]}
    n = 0
    for lid, rec in overlay.items():
        b = by_legacy.get(lid)
        if b is None or not isinstance(rec, dict):
            continue
        # Additive: remember what the building looked like BEFORE the first edit (`orig`), so a renderer that shows a
        # separate photographic mesh of the real place (photorealistic view) knows what to cut out even after a
        # restart / undo, when the published geometry is already the edited one. Absent for untouched buildings.
        if "orig" not in b:
            b["orig"] = {"outer": [list(q) for q in b["footprint"]["outer"]], "height_m": b.get("height_m"), "wall_material": ((b.get("surfaces") or {}).get("wall") or {}).get("material")}
        if isinstance(rec.get("height_m"), (int, float)):
            b["height_m"] = float(rec["height_m"])
            b["height_source"] = "user"
        if isinstance(rec.get("name"), str):
            b["name"] = rec["name"]
        if isinstance(rec.get("building_type"), str):
            b["type"] = rec["building_type"]
        if isinstance(rec.get("wall_material"), str):
            wall = b.setdefault("surfaces", {}).setdefault("wall", {})
            wall["material"], wall["source"] = rec["wall_material"], "user"
        fp = rec.get("footprint")
        if isinstance(fp, list) and len(fp) >= 3 and all(isinstance(q, dict) and "x" in q and "z" in q for q in fp):
            b["footprint"]["outer"] = [[q["x"], q["z"]] for q in fp]
            pos = rec.get("position")
            if isinstance(pos, dict) and "x" in pos and "z" in pos:
                b["centroid"] = [pos["x"], pos["z"]]
        n += 1
    return n


def partition_tiles(ds: dict) -> dict[tuple[int, int], dict]:
    """Assigns every drawable feature (whole) to the 500 m tile holding its
    first vertex / centroid."""
    tiles: dict[tuple[int, int], dict] = {}

    def tile(x: float, z: float) -> dict:
        key = (int(math.floor(x / TILE_M)), int(math.floor(z / TILE_M)))
        if key not in tiles:
            tiles[key] = {"ct_roads": [], "ct_rail_ways": [], "ct_buildings": [], "ct_ground": {"areas": [], "lines": [], "trees": []}, "ct_pois": []}
        return tiles[key]

    for r in ds["roads"]:
        slim = {k: v for k, v in r.items() if k != "node_ids"}
        tile(*r["pts"][0])["ct_roads"].append(slim)
    for w in ds["rail"]["ways"]:
        tile(*w["pts"][0])["ct_rail_ways"].append({k: v for k, v in w.items() if k != "node_ids"})
    for b in ds["buildings"]:
        tile(*b["centroid"])["ct_buildings"].append(b)
    for a in ds["ground"]["areas"]:
        tile(*a["outer"][0])["ct_ground"]["areas"].append(a)
    for l in ds["ground"]["lines"]:
        tile(*l["pts"][0])["ct_ground"]["lines"].append(l)
    for t in ds["ground"]["trees"]:
        tile(t[0], t[1])["ct_ground"]["trees"].append(t)
    for p in ds["pois"]:
        tile(*p["pos"])["ct_pois"].append(p)
    return tiles


def terrain_payload(grid: Optional[dict]) -> Optional[dict]:
    if not grid:
        return None
    heights = grid["heights_dm"]
    out = {k: v for k, v in grid.items() if k != "heights_dm"}
    out["heights_b64"] = base64.b64encode(struct.pack(f"<{len(heights)}h", *heights)).decode("ascii")
    return out


SOURCE_RANK = {"default": 0, "inferred": 1, "imagery": 2, "ai": 3, "osm": 4, "user": 5}
_HEX = __import__("re").compile(r"^#[0-9a-fA-F]{6}$")


def validate_slot_patch(patch: dict) -> tuple[dict, Optional[str]]:
    """Whitelists/validates a (partial) material slot. Returns (clean, error)."""
    clean: dict = {}
    if "material" in patch:
        if patch["material"] is not None and not isinstance(patch["material"], str):
            return {}, "material must be a string or null"
        clean["material"] = patch["material"].strip().lower()[:40] if patch["material"] else None
    if "colour" in patch:
        c = patch["colour"]
        if c is not None and not (isinstance(c, str) and _HEX.match(c)):
            return {}, "colour must be #rrggbb or null"
        clean["colour"] = c.lower() if c else None
    if "source" in patch:
        if patch["source"] not in SOURCE_RANK:
            return {}, "unknown source"
        clean["source"] = patch["source"]
    if "confidence" in patch:
        if isinstance(patch["confidence"], bool) or not isinstance(patch["confidence"], (int, float)) or not 0 <= patch["confidence"] <= 1:
            return {}, "confidence must be 0..1"
        clean["confidence"] = float(patch["confidence"])
    if not clean:
        return {}, "empty slot"
    return clean, None


def index_base_slots(ds: dict) -> dict:
    """surface_id -> the dataset's own slot, for source-rank decisions."""
    out: dict = {}

    def add(surfaces):
        for slot_ in (surfaces or {}).values():
            if isinstance(slot_, dict) and "surface_id" in slot_:
                out[slot_["surface_id"]] = {k: slot_[k] for k in ("material", "colour", "source", "confidence")}

    for r in ds["roads"]:
        add(r.get("surfaces"))
    for w in ds["rail"]["ways"]:
        add(w.get("surfaces"))
    for st in ds["rail"]["stations"]:
        add(st.get("surfaces"))
    for b in ds["buildings"]:
        add(b.get("surfaces"))
    for g in ds["ground"]["areas"]:
        out[g["surface_id"]] = {k: g["surface"][k] for k in ("material", "colour", "source", "confidence")}
    for l in ds["ground"]["lines"]:
        out[l["surface_id"]] = {k: l["surface"][k] for k in ("material", "colour", "source", "confidence")}
    base = ds["ground"]["base"]
    out[base["surface_id"]] = {k: base["surface"][k] for k in ("material", "colour", "source", "confidence")}
    return out


def compact_graph(graph: dict) -> dict:
    """Routing graph for the wire: edge polylines are kept ONLY where the edge
    has intermediate vertices (a curve); a 2-point edge is the straight line
    between its `from`/`to` node coordinates, so `pts` is omitted. Simulations
    need the curves to put cars in lanes and pedestrians on sidewalks."""
    nodes = graph["nodes"]
    edges = []
    for e in graph["edges"]:
        if len(e.get("pts", ())) <= 2:
            edges.append({k: v for k, v in e.items() if k != "pts"})
        else:
            edges.append(e)
    return {"nodes": nodes, "edges": edges}


class CityData:
    def __init__(self, world: World, name: str):
        self.world = world
        self.name = name
        self.control = world.create_entity()
        self.tile_entities: list[int] = []
        self.lock = threading.Lock()
        self.pending: Optional[dict] = None
        self.wake = threading.Event()
        self.surfaces: dict = {}
        self.base_slots: dict = {}
        self.area_id: Optional[str] = None
        self._last_progress = 0.0
        self.cancel = threading.Event()
        self.ds: Optional[dict] = None  # the dataset as published (overlay edits applied)
        self.tile_hash: dict[tuple[int, int], int] = {}
        self.tile_entity_of: dict[tuple[int, int], int] = {}
        self.last_dataset: Optional[dict] = None  # last READY city_dataset payload, restored after a failed/cancelled load
        # looks (rule + ai + osm + user appearance, see build_looks)
        self.looks: Optional[looks_lib.AreaLooks] = None
        self.looks_doc: dict = looks_lib.empty_looks(None)
        self.looks_store: Optional[looks_lib.OverridesStore] = None
        self.looks_tags: dict = {}
        self.looks_records: dict[str, dict] = {}
        self.looks_tile_ids: dict[tuple[int, int], list[str]] = {}
        self.looks_unit_tile: dict[str, tuple[int, int]] = {}
        self.looks_tile_hash: dict[tuple[int, int], int] = {}
        self.looks_meta: Optional[dict] = None

    # --- publishing helpers ---------------------------------------------------------

    def publish_areas(self) -> None:
        """`city_areas`: what the area picker can offer without any network: the curated list and every area cached on disk."""
        try:
            cached = {a["id"]: a for a in store.list_cached_areas()}
        except Exception:
            cached = {}
        out = []
        for c in CURATED_AREAS:
            out.append({**c, "cached": c["id"] in cached, "size_kb": cached.get(c["id"], {}).get("size_kb")})
        for cid, a in cached.items():
            if not any(c["id"] == cid for c in CURATED_AREAS):
                out.append({"id": cid, "name": a["name"], "description": "Downloaded earlier", "bbox": None, "cached": True, "size_kb": a.get("size_kb")})
        self.world.set_attribute(self.control, "city_areas", out)

    def progress(self, phase: str, done: int, total: int, message: Optional[str] = None) -> None:
        if self.cancel.is_set():
            raise osm_fetch.Cancelled()
        now = time.monotonic()
        if done not in (0, total) and now - self._last_progress < 0.25:
            return  # throttle: the UI needs ~4 updates/s, not one per Overpass chunk tick
        self._last_progress = now
        self.world.set_attribute(self.control, "city_progress", {"phase": phase, "done": done, "total": total, "message": message or phase})

    def _set_status(self, status: str, area: Optional[dict] = None, **extra) -> None:
        payload = {"schema_version": osm_city.SCHEMA_VERSION, "status": status}
        if area:
            payload["area"] = area
        payload.update(extra)
        if status == "ready":
            self.last_dataset = payload
        self.world.set_attribute(self.control, "city_dataset", payload)

    def publish(self, ds: dict, cache_info: dict) -> None:
        area_meta = ds["area"]
        self.area_id = area_meta["id"]
        applied = apply_building_overlay(ds, store.load_overlay(area_meta["id"], "buildings", None, {}))
        if applied:
            print(f"[{self.name}] applied {applied} saved building edit(s) from the overlay")
        self.ds = ds
        self.looks = None  # the previous area's looks must not take patches while this one is being published
        self._set_status("loading", area_meta, counts=ds.get("counts", {}))
        self.progress("publish", 0, 1, "Publishing to the city")
        surfaces = self.load_surfaces(area_meta["id"])
        self.base_slots = index_base_slots(ds)
        self.world.set_many([
            (self.control, "city_terrain", terrain_payload(ds["terrain"])),
            (self.control, "city_rail", {k: ds["rail"][k] for k in ("lines", "stations", "platforms", "entrances")}),
            (self.control, "city_rail_graph", compact_graph(ds["rail"]["graph"])),
            (self.control, "city_road_graph", compact_graph(ds["graph"])),
            (self.control, "city_surfaces", surfaces),
            (self.control, "road_attribution", area_meta["attribution"]),
            (self.control, "road_area_description", f"{area_meta['name']} — real OpenStreetMap data"),
        ])
        tiles = partition_tiles(ds)
        keys = sorted(tiles)
        looks_by_tile = self.build_looks(ds, tiles)
        self.world.set_attribute(self.control, "city_looks_meta", self.looks_meta)
        self.tile_hash = {k: hash(json.dumps(tiles[k]["ct_buildings"], sort_keys=True)) for k in keys}
        self.tile_entity_of = {k: self.tile_entities[i] for i, k in enumerate(keys)} if len(self.tile_entities) >= len(keys) else {}
        while len(self.tile_entities) < len(keys):
            self.tile_entities.append(self.world.create_entity())
        updates = []
        for index, key in enumerate(keys):
            entity = self.tile_entities[index]
            i, j = key
            data = tiles[key]
            updates.append((entity, "ct_tile", {"id": f"t_{i}_{j}", "i": i, "j": j, "bounds": {"x0": i * TILE_M, "z0": j * TILE_M, "x1": (i + 1) * TILE_M, "z1": (j + 1) * TILE_M}}))
            updates.append((entity, "ct_looks", looks_by_tile.get(key, {})))  # before ct_buildings: the renderer builds a tile once, with its looks
            for attribute, value in data.items():
                updates.append((entity, attribute, value))
            if len(updates) > 40:
                self.world.set_many(updates)
                updates = []
        self.tile_entity_of = {k: self.tile_entities[i] for i, k in enumerate(keys)}
        for entity in self.tile_entities[len(keys):]:
            updates.append((entity, "ct_tile", {"id": None, "retired": True}))
            updates.extend([(entity, "ct_looks", {}), (entity, "ct_roads", []), (entity, "ct_rail_ways", []), (entity, "ct_buildings", []), (entity, "ct_ground", {"areas": [], "lines": [], "trees": []}), (entity, "ct_pois", [])])
        if updates:
            self.world.set_many(updates)
        t = ds["terrain"]
        self._set_status(
            "ready", area_meta, counts=ds.get("counts", {}), tile_size_m=TILE_M, tile_count=len(keys), cache=cache_info,
            terrain={k: t[k] for k in ("x0", "z0", "dx", "nx", "nz", "base_elev_m", "min_m", "max_m")} if t else None,
        )
        self.progress("done", 1, 1, f"{area_meta['name']} ready")

    # --- surface overrides -----------------------------------------------------------------

    def load_surfaces(self, area_id: str) -> dict:
        path = os.path.join(store.area_dir(area_id), "surfaces.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.surfaces = json.load(f)
        else:
            self.surfaces = {}
        return self.surfaces

    def on_surface_patch(self, entity: int, attribute: str, value, source) -> None:
        """`city_surface_patch`: {surface_id, slot} or the batch form
        {patches: [{surface_id, slot}, ...], batch_id?}. Each accepted patch is
        merged per surface_id into surfaces.json; the whole batch is persisted
        once and `city_surfaces` republished once, then `city_surface_ack`
        {batch_id, accepted, rejected:[{surface_id, reason}]} says what happened.

        Source ranking (never downgrade): user > osm > ai > imagery > inferred
        > default, judged against the EFFECTIVE slot (override if present,
        else the dataset's own). A patch whose source ranks below the
        effective one is rejected, unless it carries `force: true`."""
        if entity != self.control or not isinstance(value, dict) or not self.area_id:
            return
        patches = value.get("patches") if isinstance(value.get("patches"), list) else [value]
        accepted, rejected = 0, []
        with self.lock:
            for item in patches:
                if not isinstance(item, dict):
                    continue
                sid, patch = item.get("surface_id"), item.get("slot")
                if not isinstance(sid, str) or not isinstance(patch, dict):
                    rejected.append({"surface_id": sid if isinstance(sid, str) else None, "reason": "malformed"})
                    continue
                clean, reason = validate_slot_patch(patch)
                if reason:
                    rejected.append({"surface_id": sid, "reason": reason})
                    continue
                effective = {**self.base_slots.get(sid, {}), **self.surfaces.get(sid, {})}
                new_source = clean.get("source") or effective.get("source") or "inferred"
                if not item.get("force") and SOURCE_RANK.get(new_source, 0) < SOURCE_RANK.get(effective.get("source") or "default", 0):
                    rejected.append({"surface_id": sid, "reason": f"would downgrade {effective.get('source')} to {new_source}"})
                    continue
                self.surfaces[sid] = {**self.surfaces.get(sid, {}), **clean}
                accepted += 1
            if accepted:
                path = os.path.join(store.area_dir(self.area_id), "surfaces.json")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(self.surfaces, f)
                os.replace(path + ".tmp", path)
                self.world.set_attribute(self.control, "city_surfaces", dict(self.surfaces))
            self.world.set_attribute(self.control, "city_surface_ack", {"batch_id": value.get("batch_id"), "accepted": accepted, "rejected": rejected[:50], "rejected_count": len(rejected)})

    def on_building_changed(self, entity: int, attribute: str, value, source) -> None:
        """`city_building_changed` {ids?: [legacy ids]} (write on the control entity): city-buildings validated and
        saved an edit in the shared overlay; re-apply it to the dataset and republish ONLY the tiles whose
        `ct_buildings` changed, so every renderer (3D city view included) shows height / name / type / material /
        move edits. Colours travel as surface patches instead (city_surface_patch)."""
        if entity != self.control or not self.ds or not self.area_id:
            return
        with self.lock:
            apply_building_overlay(self.ds, store.load_overlay(self.area_id, "buildings", None, {}))
            tiles = partition_tiles(self.ds)
            looks_changed = self.rebuild_looks(tiles)  # {tile key: records} where an edit changed the looks (height/type/name/move feed the rules)
            updates = []
            for key, data in tiles.items():
                h = hash(json.dumps(data["ct_buildings"], sort_keys=True))
                if key in looks_changed and key in self.tile_entity_of:
                    updates.append((self.tile_entity_of[key], "ct_looks", looks_changed[key]))  # looks first, then the buildings that use them
                if self.tile_hash.get(key) != h and key in self.tile_entity_of:
                    self.tile_hash[key] = h
                    updates.append((self.tile_entity_of[key], "ct_buildings", data["ct_buildings"]))
            if self.looks is not None and self.looks.meta(self.looks_records) != self.looks_meta:
                self.looks_meta = self.looks.meta(self.looks_records)
                updates.append((self.control, "city_looks_meta", self.looks_meta))
            if updates:
                self.world.set_many(updates)
            self.world.set_attribute(self.control, "city_building_ack", {"ids": (value or {}).get("ids") if isinstance(value, dict) else None, "tiles_updated": len([u for u in updates if u[1] == "ct_buildings"])})

    # --- looks: appearance of every building (docs/city-schema/looks.md) -------------------------------------------

    def build_looks(self, ds: dict, tiles: dict) -> dict:
        """Builds the looks of the area being published and returns {tile key: {unit id: record}} (the content of each tile's
        `ct_looks`). Rule + OSM records exist for every building even when the area has no looks.json yet; looks.json (AI) and
        looks_overrides.json (user) are layered on top. NEVER raises: looks must not stop a tile from being published, a failure
        just publishes empty looks and says why in `city_looks_meta`."""
        area_id = ds["area"]["id"]
        self.looks = None
        self.looks_records, self.looks_tile_ids, self.looks_unit_tile, self.looks_tile_hash = {}, {}, {}, {}
        try:
            area_dir = store.area_dir(area_id)
            self.looks_doc = looks_lib.read_looks(os.path.join(area_dir, looks_lib.LOOKS_FILE))
            self.looks_store = looks_lib.OverridesStore(os.path.join(area_dir, looks_lib.OVERRIDES_FILE))
            self.looks_tags = looks_lib.load_osm_tags(area_dir)
            looks = looks_lib.AreaLooks(ds, self.looks_tags, ai_units=self.looks_doc["units"], overrides=self.looks_store.data, ai_meta=self.looks_doc)
            records = looks.effective()
            by_tile = looks_lib.partition_by_tile(records, tiles)
            self._index_looks(records, by_tile)
            self.looks_meta = looks.meta(records)
            self.looks = looks
            return by_tile
        except Exception as exc:  # noqa: BLE001 - an enhancement layer: the city itself must still publish
            print(f"[{self.name}] looks failed for {area_id}: {exc!r}")
            self.looks = None
            self.looks_meta = {"schema": looks_lib.SCHEMA, "area_id": area_id, "units": 0, "by_src": {s: 0 for s in looks_lib.SOURCES}, "error": str(exc)[:200]}
            return {}

    def _index_looks(self, records: dict, by_tile: dict) -> None:
        self.looks_records = records
        self.looks_tile_ids = {k: list(v) for k, v in by_tile.items()}
        self.looks_unit_tile = {uid: k for k, v in by_tile.items() for uid in v}
        self.looks_tile_hash = {k: hash(json.dumps(v, sort_keys=True)) for k, v in by_tile.items()}

    def rebuild_looks(self, tiles: dict) -> dict:
        """After a sandbox edit changed the dataset in memory: rebuild the looks from it and return {tile key: records} for the
        tiles whose `ct_looks` changed (the caller publishes them BEFORE `ct_buildings`, so a moved building's record is always
        in the tile that holds it). Caller holds the lock."""
        if self.looks is None or self.looks_store is None:
            return {}
        try:
            looks = looks_lib.AreaLooks(self.ds, self.looks_tags, ai_units=self.looks_doc["units"], overrides=self.looks_store.data, ai_meta=self.looks_doc)
            records = looks.effective()
            by_tile = looks_lib.partition_by_tile(records, tiles)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.name}] looks rebuild failed: {exc!r}")
            return {}
        old_hash = self.looks_tile_hash
        self.looks = looks
        self._index_looks(records, by_tile)
        return {k: v for k, v in by_tile.items() if self.looks_tile_hash[k] != old_hash.get(k)}

    def republish_looks(self, ids) -> int:
        """Recomputes the effective records of `ids` and republishes `ct_looks` of exactly the tiles whose content changed (and
        `city_looks_meta` when the source counts changed). Returns the number of tiles republished. Caller holds the lock."""
        looks = self.looks  # one reference for the whole call: an area switch may replace self.looks from the worker thread
        if looks is None:
            return 0
        ids = list(ids)
        self.looks_records.update(looks.effective(ids))
        updates = []
        for key in sorted({self.looks_unit_tile[uid] for uid in ids if uid in self.looks_unit_tile}):
            entity = self.tile_entity_of.get(key)
            if entity is None:
                continue
            records = {uid: self.looks_records[uid] for uid in self.looks_tile_ids[key]}
            h = hash(json.dumps(records, sort_keys=True))
            if h != self.looks_tile_hash.get(key):
                self.looks_tile_hash[key] = h
                updates.append((entity, "ct_looks", records))
        tiles_updated = len(updates)
        meta = looks.meta(self.looks_records)
        if meta != self.looks_meta:
            self.looks_meta = meta
            updates.append((self.control, "city_looks_meta", meta))
        if updates:
            self.world.set_many(updates)
        return tiles_updated

    def on_looks_patch(self, entity: int, attribute: str, value, source) -> None:
        """`city_looks_patch` {patches: [{id, set?, clear?}], batch_id?} (or a single {id, set?, clear?}): the user's edits of
        how buildings look. `id` is a building/part id (a ROOT building id applies to all its parts, a part id to that part;
        the part wins). `clear` is applied before `set`; `set` is a partial record (nested partials allowed) validated against
        the closed vocabulary: an item with an unknown id or a bad value is rejected as a whole and never stored. Accepted
        items are persisted ONCE into looks_overrides.json (user source: always accepted, never downgraded), then only the
        touched tiles' `ct_looks` are republished, and `city_looks_ack` {batch_id, accepted, rejected: [{id, reason}],
        rejected_count, tiles_updated} says what happened."""
        if entity != self.control or not isinstance(value, dict) or not self.area_id:
            return
        patches = value.get("patches") if isinstance(value.get("patches"), list) else [value]
        batch_id = value.get("batch_id")
        with self.lock:
            looks, overrides = self.looks, self.looks_store  # one reference each: an area switch may replace them from the worker thread
            if looks is None or overrides is None:
                self.world.set_attribute(self.control, "city_looks_ack", {"batch_id": batch_id, "accepted": 0, "rejected": [{"id": None, "reason": "looks are not ready (area still loading)"}], "rejected_count": 1, "tiles_updated": 0})
                return
            new, ack, touched = looks_lib.patch_overrides(overrides.data, patches, looks.signals.by_id, batch_id)
            tiles_updated = 0
            if ack["accepted"]:
                old = overrides.data
                overrides.data = new
                try:
                    overrides.save()
                except OSError as exc:
                    overrides.data = old
                    ack = {"batch_id": batch_id, "accepted": 0, "rejected": [{"id": None, "reason": f"could not save looks_overrides.json: {exc}"}], "rejected_count": 1}
                else:
                    looks.set_overrides(new)
                    tiles_updated = self.republish_looks(looks.touched(touched))
            self.world.set_attribute(self.control, "city_looks_ack", {**ack, "tiles_updated": tiles_updated})

    def on_looks_reload(self, entity: int, attribute: str, value, source) -> None:
        """`city_looks_reload` (write anything on the control entity): re-read looks.json and looks_overrides.json from disk (for
        example after `city_appearance_ai.py generate` ran) and republish the tiles whose looks changed."""
        if entity != self.control or not self.area_id:
            return
        with self.lock:
            looks = self.looks
            if looks is None:
                return
            area_dir = store.area_dir(self.area_id)
            self.looks_doc = looks_lib.read_looks(os.path.join(area_dir, looks_lib.LOOKS_FILE))
            self.looks_store = looks_lib.OverridesStore(os.path.join(area_dir, looks_lib.OVERRIDES_FILE))
            looks.set_ai(self.looks_doc["units"], self.looks_doc)
            looks.set_overrides(self.looks_store.data)
            n = self.republish_looks(looks.ids)
            self.world.set_attribute(self.control, "city_looks_ack", {"batch_id": value.get("batch_id") if isinstance(value, dict) else None, "accepted": 0, "rejected": [], "rejected_count": 0, "reload": True, "tiles_updated": n})

    # --- area requests ------------------------------------------------------------------------

    def on_cancel(self, entity: int, attribute: str, value, source) -> None:
        if entity == self.control:
            self.cancel.set()

    def on_area_request(self, entity: int, attribute: str, value, source) -> None:
        if entity != self.control or not isinstance(value, dict):
            return
        with self.lock:
            self.pending = value  # latest request wins
        if getattr(self, "loading", False):
            self.cancel.set()  # a newer request supersedes the one in flight
        self.wake.set()

    def load_request(self, request: dict) -> None:
        try:
            area = resolve_request(request, self.progress)
            self._set_status("loading", {"id": area["id"], "name": area["name"], "bbox": area["bbox"]})
            ds, cache_info = build_area(area, self.progress, bool(request.get("force")))
            self.publish(ds, cache_info)
            store.write_active({"area": area["id"], "size_m": request.get("size_m"), "query": request.get("query")})
            self.publish_areas()
            print(f"[{self.name}] {area['name']}: {ds['counts']} ({'cache' if cache_info['hit'] else 'downloaded'}, {cache_info['seconds']}s)")
        except (osm_fetch.Cancelled, Exception) as exc:  # report to the UI, keep serving the previous area
            cancelled = isinstance(exc, osm_fetch.Cancelled)
            message = "Cancelled" if cancelled else str(exc)
            print(f"[{self.name}] area load {'cancelled' if cancelled else 'failed'}: {exc!r}")
            if self.last_dataset:  # the previous city is still fully published: show it again, with a notice
                self.world.set_attribute(self.control, "city_dataset", {**self.last_dataset, "notice": message if not cancelled else None, "error": None if cancelled else message})
            else:
                self._set_status("error", None, error=message, request_id=request.get("request_id"))
            self.cancel.clear()
            self.world.set_attribute(self.control, "city_progress", {"phase": "done", "done": 1, "total": 1, "message": message, "ok": False})

    def worker(self) -> None:
        while True:
            self.wake.wait()
            self.wake.clear()
            with self.lock:
                request, self.pending = self.pending, None
            if request:
                self.cancel.clear()
                self.loading = True
                try:
                    self.load_request(request)
                finally:
                    self.loading = False


def main():
    with World() as world:
        name = world.identify("city-data")
        city = CityData(world, name)
        world.set_attribute(city.control, "city_dataset", {"schema_version": osm_city.SCHEMA_VERSION, "status": "loading", "area": None})
        city.publish_areas()
        world.subscribe("city_area_request", city.on_area_request)
        world.subscribe("city_surface_patch", city.on_surface_patch)
        world.subscribe("city_building_changed", city.on_building_changed)
        world.subscribe("city_looks_patch", city.on_looks_patch)
        world.subscribe("city_looks_reload", city.on_looks_reload)
        world.subscribe("city_area_cancel", city.on_cancel)
        threading.Thread(target=city.worker, daemon=True).start()
        active = store.read_active()
        city.on_area_request(city.control, "city_area_request", {"area_id": active["area"], "query": active.get("query"), "size_m": active.get("size_m"), "request_id": "startup"}, None)
        print(f"[{name}] ready - active area {active['area']!r}; send city_area_request to change it")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
