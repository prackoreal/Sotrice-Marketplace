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

import copy
import json
import os
import threading
import time

import agent_core
import chat_store
import city_agent
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
            "area_description": f"{ds['area']['name']} \u2014 real OpenStreetMap data", "real": True, "ds": ds,
        }
    path = os.path.join(DATA_DIR, f"{area}_buildings.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data["buildings"], {"attribution": data["attribution"], "license": data["license"], "area_description": data["area_description"] + " (small demo extract)", "real": False}


HISTORY_LIMIT = 100
SLOT_KEYS = ("material", "colour", "source", "confidence")
# move_parts: building_move_request may carry parts:[{entity,new_x,new_z}] (a split building moves as ONE undo step).
CAPABILITIES = {"move_parts": True}


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
        self.view_state: dict = {}            # latest `city_view_state` from the city view (what the user looks at)
        self.knowledge = None                 # city_agent.CityKnowledge for the active area
        self.city_control = None              # city-data's control entity (surface patches / building_changed / area requests go there)
        self.extra: dict = {}                 # entity -> {traffic_visits, nearest_road, road_distance_m} from economics-test
        self.areas: list = []                 # `city_areas`: what the area picker offers (curated + cached), for the agent's switch_area
        self.tally = city_agent.EntityTally() # live agent counts (walkers / vehicles ...) for the agent's context and count_entities
        self.entity_of_id: dict = {}          # legacy building id -> entity
        # Undo/redo: one step per edit command (a move, a field edit, one AI/voice command). Each change keeps the
        # building plus the overlay values / colour slots before and after, so both directions re-apply the same way.
        self.undo_stack: list = []
        self.redo_stack: list = []
        self.slot_cache: dict = {}            # surface_id -> last slot THIS plugin wrote (the surfaces on the building dict go stale)
        # What this plugin version understands, for UIs that may be older or newer than it (plugins update independently).
        # A UI only sends a request shape the plugin advertises here; without the attribute (old plugin) it falls back to
        # the original one-request-per-part shape, which every version accepts.
        world.set_attribute(self.control, "building_capabilities", CAPABILITIES)

    # --- publishing -------------------------------------------------------------------

    def load(self, area: str) -> bool:
        loaded = load_area_buildings(area)
        if loaded is None:
            return False
        buildings, meta = loaded
        with self.lock:
            self.area = area
            self.undo_stack, self.redo_stack, self.slot_cache = [], [], {}
            self._publish_history()
            self.overlay = load_buildings_overlay(area)
            for building in buildings:
                moved = self.overlay.get(building["id"])
                if moved:
                    for key in ("position", "footprint", "wall_material", "height_m", "name", "building_type", "goods_price_eur"):
                        if key in moved:
                            building[key] = moved[key]
            self.building_by_entity = {}
            self.entity_of_id = {}
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
                self.entity_of_id[building["id"]] = entity
                updates.extend(self._attributes(entity, building))
                if not building.get("wall_material"):
                    unknown.append((entity, building))
                if len(updates) >= BATCH:
                    self.world.set_many(updates)
                    updates = []
            self.knowledge = city_agent.CityKnowledge(meta["ds"], {b["id"]: b for b in buildings}) if meta.get("ds") else None
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

    def on_view_state(self, entity, attribute, value, source):
        if isinstance(value, dict):
            self.view_state = value

    def on_economics(self, entity, attribute, value, source):
        """economics-test's derived per-building numbers (traffic visits, nearest road), kept for the AI agent."""
        key = {"building_traffic_visits_count": "traffic_visits", "building_nearest_road_name": "nearest_road", "building_road_distance_m": "road_distance_m"}.get(attribute)
        if key and entity in self.building_by_entity:
            self.extra.setdefault(entity, {})[key] = value

    def building_extra(self, building: dict) -> dict:
        return self.extra.get(self.entity_of_id.get(building["id"]), {})

    def on_areas(self, entity, attribute, value, source):
        if isinstance(value, list):
            self.areas = [a for a in value if isinstance(a, dict) and a.get("id") and a.get("name")]
            if self.city_control is None:
                self.city_control = entity

    def request_area(self, request: dict) -> str:
        """The area picker's own request (city-data loads / downloads it and republishes everything). Returns at once:
        a new area can take minutes to download, the city view shows the progress."""
        if self.city_control is None:
            raise RuntimeError("the city data plugin (city-data) is not running")
        self.world.set_attribute(self.city_control, "city_area_request", {**request, "size_m": None})
        return "area switch requested; the city view shows the progress"

    def on_dataset(self, entity, attribute, value, source):
        if not isinstance(value, dict) or value.get("status") != "ready":
            return
        self.city_control = entity
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

    # --- undo / redo -------------------------------------------------------------------

    def _publish_history(self) -> None:
        self.world.set_attribute(self.control, "building_history_state", {
            "can_undo": bool(self.undo_stack), "can_redo": bool(self.redo_stack),
            "undo_label": self.undo_stack[-1]["label"] if self.undo_stack else None,
            "redo_label": self.redo_stack[-1]["label"] if self.redo_stack else None,
        })

    def _push_step(self, label: str, changes: list) -> None:
        if not changes:
            return
        self.undo_stack.append({"label": label, "changes": changes})
        del self.undo_stack[:-HISTORY_LIMIT]
        self.redo_stack.clear()
        self._publish_history()

    def _slot_now(self, building: dict, part: str):
        slot = (building.get("surfaces") or {}).get(part) or {}
        sid = slot.get("surface_id")
        if not sid:
            return None
        return sid, dict(self.slot_cache.get(sid) or {k: slot.get(k) for k in SLOT_KEYS})

    def _restore(self, step: dict, which: str) -> None:
        """Re-applies one side ("before" to undo, "after" to redo) of a step through the same publish + persist +
        city-data patch route as a normal edit."""
        with self.lock:
            updates, ids, patches = [], [], []
            for ch in step["changes"]:
                b = ch["building"]
                entity = self.entity_of_id.get(b["id"])
                for key, value in ch["values"][which].items():
                    value = copy.deepcopy(value)
                    b[key] = value
                    self.overlay.setdefault(b["id"], {})[key] = value
                    if entity is None:
                        continue
                    if key == "position":
                        updates.append((entity, "building_position", {"x": value["x"], "y": 0.0, "z": value["z"]}))
                    elif key == "footprint":
                        updates.append((entity, "building_footprint", value))
                    else:
                        field = self._field_of(key)
                        if field:
                            updates.append((entity, field, value))
                            if field == "building_material":
                                updates.append((entity, "building_material_source", "history"))
                ids.append(b["id"])
                for sid, pair in (ch.get("slots") or {}).items():
                    slot = pair[which]
                    clean = {"material": slot.get("material"), "colour": slot.get("colour"), "source": slot.get("source") or "default", "confidence": slot.get("confidence") or 0.0}
                    patches.append({"surface_id": sid, "slot": clean, "force": True})
                    self.slot_cache[sid] = dict(slot)
            for i in range(0, len(updates), BATCH):
                self.world.set_many(updates[i:i + BATCH])
            save_buildings_overlay(self.area, self.overlay)
            if self.city_control is not None:
                self.world.set_attribute(self.city_control, "city_building_changed", {"ids": ids})
                for i in range(0, len(patches), 100):
                    self.world.set_attribute(self.city_control, "city_surface_patch", {"patches": patches[i:i + 100], "batch_id": f"history-{int(time.time() * 1000)}-{i}"})

    def history_action(self, action) -> tuple:
        """Undo / redo one edit step. Shared by the city view's Ctrl+Z / Ctrl+Y and the AI agent. -> (ok, message)"""
        with self.lock:
            ok, message = False, "Nothing to " + str(action)
            if action == "undo" and self.undo_stack:
                step = self.undo_stack.pop()
                self._restore(step, "before")
                self.redo_stack.append(step)
                ok, message = True, "Undid: " + step["label"]
            elif action == "redo" and self.redo_stack:
                step = self.redo_stack.pop()
                self._restore(step, "after")
                self.undo_stack.append(step)
                ok, message = True, "Redid: " + step["label"]
            self._publish_history()
            print(f"[{self.name}] history {action}: {message}")
            return ok, message

    def on_history_request(self, entity, attribute, value, source):
        """The city view's Undo / Redo (Ctrl+Z / Ctrl+Y): {"action": "undo"|"redo", "nonce": any}."""
        if not isinstance(value, dict):
            return
        ok, message = self.history_action(value.get("action"))
        self.world.set_attribute(self.control, "building_history_result", {"nonce": value.get("nonce"), "ok": ok, "message": message})

    # --- shared, validated apply paths (the AI agent uses these; the UI paths below keep their own handlers) ---

    def edit_fields(self) -> dict:
        return ALLOWED_EDIT_FIELDS

    def _field_of(self, overlay_key: str):
        for field, spec in ALLOWED_EDIT_FIELDS.items():
            if spec["overlay_key"] == overlay_key:
                return field
        return None

    def apply_building_edits(self, items, colour=None) -> str:
        """items: [(building, {overlay_key: validated value})]. Publishes the legacy attributes, persists ONE
        overlay write, tells city-data (tiles) and patches wall+roof colour slots (source "user")."""
        with self.lock:
            updates, ids, changes = [], [], []
            for building, fields in items:
                slots = {}
                if colour:
                    for part in ("wall", "roof"):
                        now = self._slot_now(building, part)
                        if now:
                            slots[now[0]] = {"before": now[1], "after": dict(now[1], colour=colour, source="user", confidence=1.0)}
                if fields or slots:
                    changes.append({"building": building, "values": {"before": {k: copy.deepcopy(building.get(k)) for k in fields}, "after": copy.deepcopy(fields)}, "slots": slots})
            for building, fields in items:
                entity = self.entity_of_id.get(building["id"])
                for overlay_key, value in fields.items():
                    building[overlay_key] = value
                    self.overlay.setdefault(building["id"], {})[overlay_key] = value
                    field = self._field_of(overlay_key)
                    if entity is not None and field:
                        updates.append((entity, field, value))
                        if field == "building_material":
                            updates.append((entity, "building_material_source", "agent_edit"))
                ids.append(building["id"])
            for i in range(0, len(updates), BATCH):
                self.world.set_many(updates[i:i + BATCH])
            if any(fields for _b, fields in items):
                save_buildings_overlay(self.area, self.overlay)
            if self.city_control is not None and any(fields for _b, fields in items):
                self.world.set_attribute(self.city_control, "city_building_changed", {"ids": ids})
            painted = 0
            if colour and self.city_control is not None:
                patches = []
                for building, _f in items:
                    for part in ("wall", "roof"):
                        slot = (building.get("surfaces") or {}).get(part) or {}
                        if slot.get("surface_id"):
                            patches.append({"surface_id": slot["surface_id"], "slot": {"colour": colour, "source": "user", "confidence": 1.0}, "force": True})
                for i in range(0, len(patches), 100):
                    self.world.set_attribute(self.city_control, "city_surface_patch", {"patches": patches[i:i + 100], "batch_id": f"agent-{int(time.time() * 1000)}-{i}"})
                painted = len(patches)
                for ch in changes:
                    for sid, pair in ch["slots"].items():
                        self.slot_cache[sid] = dict(pair["after"])
            else:
                for ch in changes:
                    ch["slots"] = {}
            self._push_step(f"AI edit ({len(items)} building{'s' if len(items) != 1 else ''})", changes)
            print(f"[{self.name}] agent edit: {len(items)} building(s), {painted} colour slot(s)")
            return f"{len(items)} buildings updated" + (f", {painted} surfaces painted" if painted else "")

    def move_building_by(self, building, dx: float, dz: float) -> str:
        with self.lock:
            entity = self.entity_of_id.get(building["id"])
            old = building["position"]
            new_position = {"x": old["x"] + dx, "z": old["z"] + dz}
            new_footprint = [{"x": q["x"] + dx, "z": q["z"] + dz} for q in building["footprint"]]
            self._push_step("Move building", [{"building": building, "values": {"before": {"position": copy.deepcopy(old), "footprint": copy.deepcopy(building["footprint"])}, "after": {"position": copy.deepcopy(new_position), "footprint": copy.deepcopy(new_footprint)}}}])
            building["position"], building["footprint"] = new_position, new_footprint
            if entity is not None:
                self.world.set_many([(entity, "building_position", {"x": new_position["x"], "y": 0.0, "z": new_position["z"]}), (entity, "building_footprint", new_footprint)])
            record = self.overlay.setdefault(building["id"], {})
            record["position"], record["footprint"] = new_position, new_footprint
            save_buildings_overlay(self.area, self.overlay)
            if self.city_control is not None:
                self.world.set_attribute(self.city_control, "city_building_changed", {"ids": [building["id"]]})
            return f"moved building {building['id']}"

    def on_move_request(self, entity, attribute, value, source):
        """Sandbox mode: rendering-test-ui writes this on a mouse drag-drop. The
        UI only shapes the request (new_x/new_z); this does the work and owns
        persistence. Rigid translation only, no rotation.

        A building split into parts (Vienna: dozens of part entities) moves in ONE request: `parts` lists
        [{"entity", "new_x", "new_z"}, ...] (the request's own new_x/new_z is the primary part, kept so a request
        without `parts` works exactly as before). The whole move is ONE undo step, ONE overlay write and ONE batch of
        attribute updates, so renderers rebuild the touched tiles once, not once per part."""
        if not isinstance(value, dict):
            return
        moves = [(entity, value.get("new_x"), value.get("new_z"))]
        parts = value.get("parts")
        if isinstance(parts, list):
            moves = [(p.get("entity"), p.get("new_x"), p.get("new_z")) for p in parts if isinstance(p, dict)] or moves
        with self.lock:
            changes, updates, seen = [], [], set()
            for ent, new_x, new_z in moves:
                building = self.building_by_entity.get(ent)
                if building is None or ent in seen:
                    continue
                if not isinstance(new_x, (int, float)) or not isinstance(new_z, (int, float)):
                    continue
                seen.add(ent)
                old = building["position"]
                dx, dz = new_x - old["x"], new_z - old["z"]
                new_position = {"x": new_x, "z": new_z}
                new_footprint = [{"x": p["x"] + dx, "z": p["z"] + dz} for p in building["footprint"]]
                if abs(dx) + abs(dz) > 0.01:
                    changes.append({"building": building, "values": {"before": {"position": copy.deepcopy(old), "footprint": copy.deepcopy(building["footprint"])}, "after": {"position": copy.deepcopy(new_position), "footprint": copy.deepcopy(new_footprint)}}})
                building["position"] = new_position
                building["footprint"] = new_footprint
                updates.append((ent, "building_position", {"x": new_x, "y": 0.0, "z": new_z}))
                updates.append((ent, "building_footprint", new_footprint))
                # Merge into whatever's already there: a previously voice-edited
                # building that is now ALSO dragged must keep both sets of changes.
                record = self.overlay.setdefault(building["id"], {})
                record["position"] = new_position
                record["footprint"] = new_footprint
            if not updates:
                return
            self._push_step("Move building" if len(changes) <= 1 else f"Move building ({len(changes)} parts)", changes)
            for i in range(0, len(updates), BATCH):
                self.world.set_many(updates[i:i + BATCH])
            save_buildings_overlay(self.area, self.overlay)
            print(f"[{self.name}] moved {len(seen)} building part(s) in one request (first entity {moves[0][0]}), persisted to overlay")

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
            self._push_step(f"Edit {field.replace('building_', '')}", [{"building": building, "values": {"before": {overlay_key: copy.deepcopy(building.get(overlay_key))}, "after": {overlay_key: copy.deepcopy(result)}}}])
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
        world.subscribe("building_history_request", pub.on_history_request)
        world.subscribe("city_dataset", pub.on_dataset, replay=True)
        world.subscribe("city_areas", pub.on_areas, replay=True)
        for attr in ("building_traffic_visits_count", "building_nearest_road_name", "building_road_distance_m"):
            world.subscribe(attr, pub.on_economics, replay=True)
        world.subscribe("agent_type", pub.tally.on_type, replay=True)
        world.subscribe("vehicle_kind", pub.tally.on_kind, replay=True)
        world.subscribe("agent_state", pub.tally.on_state, replay=True)
        world.subscribe("city_view_state", pub.on_view_state, replay=True)
        area = active_area()
        if not pub.load(area):
            print(f"[{name}] no data for area {area!r} yet - waiting for city-data to download it (city_dataset)")
        # The city AI agent (agent_core.SpinAgent): sees only its allowlist (area aggregate + what the user looks at),
        # edits only through the validated paths above. Control entity = this plugin's own control entity.
        allowlist, context, tools, system = city_agent.make_agent_parts(pub)
        agent = agent_core.SpinAgent(world, spin_id="city", label="City", allowlist=allowlist, context_provider=context, tools=tools, system_prompt=system, control_entity=pub.control, chats=chat_store.ChatStore(scope="city"))
        agent.start()
        print(f"[{name}] city agent ready: tools {sorted(agent.tools)}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
