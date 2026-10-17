"""city-buildings — one entity per REAL building (OpenStreetMap footprints,
heights, types, materials where tagged) for the active area, published as the
attributes every existing consumer already reads (building_footprint,
building_position, building_height_m, building_material, building_type, ...).

Data source: the cached schema-v1 dataset city-data downloads for the active
area (docs/city-schema/schema.md) — any place on Earth, not the old 15-
building demo. If the active area has no dataset yet (offline first run) the
original small demo extract in city_roads_data/ is used, labelled as such.

* Live area switching: `city_dataset` (status ready, new area id) => this
  plugin republishes. Core has no entity deletion, so building entities are
  POOLED: reused for the new area's buildings, surplus ones are retired
  (`kind = "retired"`, empty footprint).
* Footprint HOLES and the richer schema fields are published as new optional
  attributes (building_id, building_levels, building_min_height_m,
  building_base_y, building_roof, building_has_parts, building_part_of,
  building_surfaces, building_footprint_holes). Parent outlines of buildings
  that have `building:part`s are not published: the parts replace them.
* Sandbox edits (move / voice edits) work exactly as before and persist per
  area in the area's cache folder (overlay_buildings.json); the AI/voice edit
  path stays the ONLY writer (ALLOWED_EDIT_FIELDS validators below).
* AI wall-material classification: only for small areas (<= AI_CLASSIFY_MAX
  unknown-material buildings). For a real city the looks pipeline
  (city_surface_patch, schema section 6) fills materials in efficient batches;
  firing one Gemini request per building of a real city would be thousands of
  billed requests.

Registered in plugins/registry.json as "city-buildings".
"""

import json
import os
import threading
import time

import citydata_store as store
from sotrice_client import World

DATA_DIR = store.LEGACY_DIR
ACTIVE_AREA_PATH = store.LEGACY_ACTIVE
DEFAULT_AREA = store.DEFAULT_AREA
AI_CLASSIFY_MAX = 25  # beyond this, leave materials to the looks pipeline (cost)
BATCH = 400  # attribute writes per set_many

KNOWN_MATERIALS = {"brick", "concrete", "stucco", "wood", "glass", "stone", "metal"}


def _validate_material(value):
    if not isinstance(value, str):
        return False, "building_material must be a string"
    cleaned = value.strip().lower()
    if cleaned not in KNOWN_MATERIALS:
        return False, f"{value!r} isn't a supported material (try: {', '.join(sorted(KNOWN_MATERIALS))})"
    return True, cleaned


def _validate_height(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, "building_height_m must be a number"
    cleaned = float(value)
    if not (1.0 <= cleaned <= 500.0):
        return False, "building_height_m must be between 1 and 500 meters"
    return True, cleaned


def _validate_name(value):
    if not isinstance(value, str) or not value.strip():
        return False, "building_name must be a non-empty string"
    cleaned = value.strip()
    if len(cleaned) > 120:
        return False, "building_name must be 120 characters or fewer"
    return True, cleaned


# A real OSM `building=*` tag is free text (e.g. "yes", "house",
# "retail" -- see scripts/extract_city_buildings.py), not a fixed
# vocabulary, so this is deliberately a smaller, curated DESIGNATION list
# for what a user can deliberately set a building TO, not a mirror of
# every raw OSM value this project happens to import. Per-building
# economics (see economics_test.py's building_revenue_estimate_eur) only
# activates for "shop" today -- the others are real, published, and
# renderable, just not yet economically modeled, same "build what's
# genuinely demonstrable first" discipline as every other skeleton here.
KNOWN_BUILDING_TYPES = {"shop", "residential", "office", "restaurant", "warehouse", "industrial", "civic", "other"}


def _validate_building_type(value):
    if not isinstance(value, str):
        return False, "building_type must be a string"
    cleaned = value.strip().lower()
    if cleaned not in KNOWN_BUILDING_TYPES:
        return False, f"{value!r} isn't a supported building type (try: {', '.join(sorted(KNOWN_BUILDING_TYPES))})"
    return True, cleaned


def _validate_goods_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, "building_goods_price_eur must be a number"
    cleaned = float(value)
    if not (0.01 <= cleaned <= 1_000_000.0):
        return False, "building_goods_price_eur must be between 0.01 and 1,000,000"
    return True, round(cleaned, 2)


# The whitelist of what a building_edit_request is actually allowed to
# touch, and how each one is validated -- deliberately small, each entry
# independently demonstrable. This exists so an untrusted AI-generated
# edit suggestion (see handle_edit_request below) can be rejected with
# an honest reason instead of ever reaching world.set_attribute with an
# arbitrary attribute name/value. "overlay_key" is the key this field is
# stored under in the per-building overlay record and in the in-memory
# building dict (mirrors the real OSM extract's own field names where
# one already exists, e.g. "wall_material", not the published attribute
# name "building_material").
ALLOWED_EDIT_FIELDS = {
    "building_material": {"overlay_key": "wall_material", "validate": _validate_material},
    "building_height_m": {"overlay_key": "height_m", "validate": _validate_height},
    "building_name": {"overlay_key": "name", "validate": _validate_name},
    "building_type": {"overlay_key": "building_type", "validate": _validate_building_type},
    "building_goods_price_eur": {"overlay_key": "goods_price_eur", "validate": _validate_goods_price},
}


def active_area() -> str:
    return store.read_active()["area"]


def legacy_overlay_path(area: str) -> str:
    return os.path.join(DATA_DIR, f"{area}_buildings_overlay.json")


def load_buildings_overlay(area: str) -> dict:
    """Sandbox edits keyed by the building's stable OSM id (NOT the ECS entity
    id, which isn't stable across restarts). Separate from the extract itself,
    which stays immutable: this is Sotrice-generated sandbox state."""
    return store.load_overlay(area, "buildings", legacy_overlay_path(area), {})


def save_buildings_overlay(area: str, overlay: dict) -> None:
    store.save_overlay(area, "buildings", overlay)


def load_area_buildings(area: str):
    """-> (buildings, meta) or None. Buildings are in the legacy dict shape
    (store.legacy_buildings) with the extra schema fields."""
    ds = store.load_dataset(area)
    if ds is not None:
        return store.legacy_buildings(ds), {
            "attribution": ds["area"]["attribution"], "license": ds["area"]["license"],
            "area_description": f"{ds['area']['name']} \u2014 real OpenStreetMap data", "real": True,
        }
    path = os.path.join(DATA_DIR, f"{area}_buildings.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["buildings"], {"attribution": data["attribution"], "license": data["license"], "area_description": data["area_description"] + " (small demo extract)", "real": False}


class BuildingPublisher:
    def __init__(self, world: World, name: str):
        self.world = world
        self.name = name
        self.control = world.create_entity()
        self.pool: list[int] = []
        self.building_by_entity: dict[int, dict] = {}
        self.area = None
        self.overlay: dict = {}
        self.lock = threading.RLock()

    # --- publishing -------------------------------------------------------------------

    def load(self, area: str) -> bool:
        loaded = load_area_buildings(area)
        if loaded is None:
            return False
        buildings, meta = loaded
        with self.lock:
            self.area = area
            self.overlay = load_buildings_overlay(area)
            for building in buildings:
                moved = self.overlay.get(building["id"])
                if moved:
                    for key in ("position", "footprint", "wall_material", "height_m", "name", "building_type", "goods_price_eur"):
                        if key in moved:
                            building[key] = moved[key]
            self.building_by_entity = {}
            while len(self.pool) < len(buildings):
                self.pool.append(self.world.create_entity())
            real_heights = sum(1 for b in buildings if b["height_source"] == "tag")
            print(f"[{self.name}] {meta['area_description']} \u2014 {len(buildings)} buildings ({real_heights} with a real OSM height tag, "
                  f"{len(buildings) - real_heights} estimated) ({meta['attribution']}, {meta['license']})")
            self.world.set_many([
                (self.control, "building_attribution", meta["attribution"]),
                (self.control, "building_area_description", meta["area_description"]),
            ])
            unknown = []
            updates = []
            for index, building in enumerate(buildings):
                entity = self.pool[index]
                self.building_by_entity[entity] = building
                updates.extend(self._attributes(entity, building))
                if not building.get("wall_material"):
                    unknown.append((entity, building))
                if len(updates) >= BATCH:
                    self.world.set_many(updates)
                    updates = []
            for entity in self.pool[len(buildings):]:
                updates.extend([(entity, "kind", "retired"), (entity, "building_footprint", [])])
            if updates:
                self.world.set_many(updates)
            if 0 < len(unknown) <= AI_CLASSIFY_MAX:
                for entity, building in unknown:
                    self._request_material(entity, building)
            elif unknown:
                print(f"[{self.name}] {len(unknown)} buildings without a wall material: leaving them to the looks pipeline "
                      f"(AI classification is only automatic for <= {AI_CLASSIFY_MAX}; one request per building would be billed per building)")
        return True

    def _attributes(self, entity: int, b: dict) -> list:
        ups = [
            (entity, "kind", "building"),
            (entity, "building_osm_id", b["id"]),
            (entity, "building_footprint", b["footprint"]),
            (entity, "building_position", {"x": b["position"]["x"], "y": 0.0, "z": b["position"]["z"]}),
            (entity, "building_height_m", b["height_m"]),
            (entity, "building_height_source", b["height_source"]),
        ]
        if b.get("wall_material"):
            ups.append((entity, "building_material", b["wall_material"]))
        if b.get("building_type"):
            ups.append((entity, "building_type", b["building_type"]))
        if b.get("name"):
            ups.append((entity, "building_name", b["name"]))
        if b.get("goods_price_eur") is not None:
            ups.append((entity, "building_goods_price_eur", b["goods_price_eur"]))
        if "dataset_id" in b:  # real dataset: the richer schema-v1 fields
            ups.extend([
                (entity, "building_id", b["dataset_id"]),
                (entity, "building_levels", b.get("levels")),
                (entity, "building_min_height_m", b.get("min_height_m", 0.0)),
                (entity, "building_base_y", b.get("base_y", 0.0)),
                (entity, "building_roof", b.get("roof")),
                (entity, "building_has_parts", False),
                (entity, "building_part_of", b.get("part_of")),
                (entity, "building_surfaces", b.get("surfaces")),
                (entity, "building_footprint_holes", b.get("holes") or []),
            ])
        return ups

    def _request_material(self, entity: int, building: dict) -> None:
        # ai-test is domain-agnostic; WE do the building-specific prompt-shaping
        # (the "calling spin shapes the request" rule of the universal AI spin).
        self.world.set_attribute(entity, "ai_request", {
            "prompt": (
                "You are classifying the primary exterior wall material of a real building for a "
                "city-simulation renderer. Reply with ONLY one word from this list: brick, concrete, stucco, wood, "
                "glass, stone, metal. Base your answer on the building type, height, and footprint size given in the context."
            ),
            "context": {"building_type": building.get("building_type"), "height_m": building["height_m"],
                        "height_source": building["height_source"], "footprint": building["footprint"]},
            "request_id": f"wall-material-{entity}",
            "task_hint": "simple",
        })

    # --- handlers ----------------------------------------------------------------------

    def on_dataset(self, entity, attribute, value, source):
        if not isinstance(value, dict) or value.get("status") != "ready":
            return
        area = (value.get("area") or {}).get("id")
        if area and area != self.area:
            self.load(area)

    def on_ai_response(self, entity, attribute, value, source):
        if not isinstance(value, dict):
            return
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or not request_id.startswith("wall-material-"):
            return
        text = value.get("text")
        if not text or entity not in self.building_by_entity:
            return
        material = text.strip().split()[0].strip(".,").lower()
        self.world.set_attribute(entity, "building_material", material)
        self.world.set_attribute(entity, "building_material_source", "ai")

    def on_move_request(self, entity, attribute, value, source):
        """Sandbox mode: rendering-test-ui writes this on a mouse drag-drop. The
        UI only shapes the request (new_x/new_z); this does the work and owns
        persistence. Rigid translation only, no rotation."""
        if not isinstance(value, dict):
            return
        with self.lock:
            building = self.building_by_entity.get(entity)
            if building is None:
                return
            new_x, new_z = value.get("new_x"), value.get("new_z")
            if not isinstance(new_x, (int, float)) or not isinstance(new_z, (int, float)):
                return
            old = building["position"]
            dx, dz = new_x - old["x"], new_z - old["z"]
            new_position = {"x": new_x, "z": new_z}
            new_footprint = [{"x": p["x"] + dx, "z": p["z"] + dz} for p in building["footprint"]]
            building["position"] = new_position
            building["footprint"] = new_footprint
            self.world.set_attribute(entity, "building_position", {"x": new_x, "y": 0.0, "z": new_z})
            self.world.set_attribute(entity, "building_footprint", new_footprint)
            # Merge into whatever's already there: a previously voice-edited
            # building that is now ALSO dragged must keep both sets of changes.
            record = self.overlay.setdefault(building["id"], {})
            record["position"] = new_position
            record["footprint"] = new_footprint
            save_buildings_overlay(self.area, self.overlay)
            print(f"[{self.name}] building {building['id']} (entity {entity}) moved to ({new_x:.1f}, {new_z:.1f}), persisted to overlay")

    def on_edit_request(self, entity, attribute, value, source):
        """Voice/AI-driven single-building editing: the calling UI never writes
        building_material/height/name from an AI suggestion — it proposes a
        field+value and THIS handler is the only thing that validates and
        applies it (an LLM's output is untrusted input)."""
        if not isinstance(value, dict):
            return
        with self.lock:
            building = self.building_by_entity.get(entity)
            if building is None:
                return
            request_id = value.get("request_id")
            field = value.get("field")
            spec = ALLOWED_EDIT_FIELDS.get(field) if isinstance(field, str) else None
            if spec is None:
                self.world.set_attribute(entity, "building_edit_result", {"request_id": request_id, "ok": False, "message": f"{field!r} isn't an editable building property yet."})
                return
            ok, result = spec["validate"](value.get("value"))
            if not ok:
                self.world.set_attribute(entity, "building_edit_result", {"request_id": request_id, "ok": False, "message": result})
                return
            overlay_key = spec["overlay_key"]
            building[overlay_key] = result
            self.world.set_attribute(entity, field, result)
            if field == "building_material":
                self.world.set_attribute(entity, "building_material_source", "voice_edit")
            record = self.overlay.setdefault(building["id"], {})
            record[overlay_key] = result
            save_buildings_overlay(self.area, self.overlay)
            self.world.set_attribute(entity, "building_edit_result", {"request_id": request_id, "ok": True, "message": f"Updated {field} to {result!r}."})
            print(f"[{self.name}] building {building['id']} (entity {entity}) {field} -> {result!r} via voice edit, persisted")


def main():
    with World() as world:
        name = world.identify("city-buildings")
        pub = BuildingPublisher(world, name)
        world.subscribe("ai_response", pub.on_ai_response, replay=True)
        world.subscribe("building_move_request", pub.on_move_request, replay=True)
        world.subscribe("building_edit_request", pub.on_edit_request, replay=True)
        world.subscribe("city_dataset", pub.on_dataset, replay=True)
        area = active_area()
        if not pub.load(area):
            print(f"[{name}] no data for area {area!r} yet - waiting for city-data to download it (city_dataset)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
