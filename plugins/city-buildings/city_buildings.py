"""city-buildings — a real, minimal building-FOOTPRINT data source, for
the SAME small Klosterneuburg area city-roads already covers. Loads a
real OpenStreetMap extract (see `scripts/extract_city_buildings.py` for
how it was produced and `clients/python/city_roads_data/
klosterneuburg_center_buildings.json` for the data itself — aligned to
the SAME local coordinate origin as `city_roads.py`'s road network, so
the two line up spatially) and publishes one entity per real building:
its footprint polygon, ground-level position (centroid), and height —
real where OSM had a height/building:levels tag, a documented estimate
where it didn't (see `building_height_source`).

This is deliberately the keyless, free, ODbL-licensed OSM footprint
problem — plain 2D outlines plus a height number — NOT the
photorealistic Google 3D Tiles / Cesium ingestion, which needs a real
external account/API key decision this file has no business making.
Unblocks a future economics spin (needs real building entities to
compute against) and gives a future rendering spin real building shapes
to draw — this file has no idea whether either is running, the same
ignorance-of-the-consumer every other watch/write plugin here already
has.

No live API calls at runtime — same "no live API" discipline as
city_roads.py and this project's LaMa integration.

Registered in plugins/registry.json as "city-buildings".
"""

import json
import os
import time

from sotrice_client import World

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "city_roads_data")
ACTIVE_AREA_PATH = os.path.join(DATA_DIR, "active_area.json")
DEFAULT_AREA = "klosterneuburg_center"

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
}


def active_area() -> str:
    """Same active-area config city_roads.py reads — see that file's own
    doc comment. Kept as its own small function here (not a shared
    import) since these two plugins are each meant to run standalone,
    with no dependency on each other's module."""
    if os.path.exists(ACTIVE_AREA_PATH):
        with open(ACTIVE_AREA_PATH, encoding="utf-8") as f:
            return json.load(f).get("area", DEFAULT_AREA)
    return DEFAULT_AREA


def load_building_data():
    data_path = os.path.join(DATA_DIR, f"{active_area()}_buildings.json")
    with open(data_path, encoding="utf-8") as f:
        return json.load(f)


def overlay_path(area: str) -> str:
    return os.path.join(DATA_DIR, f"{area}_buildings_overlay.json")


def load_buildings_overlay(area: str) -> dict:
    """Sandbox-mode moved-building positions, keyed by the building's
    stable real OSM way id (NOT the ECS entity id, which isn't stable
    across restarts). Kept entirely separate from the real OSM extract
    file, which stays immutable/untouched — this is Sotrice-generated
    sandbox state, not OpenStreetMap data."""
    path = overlay_path(area)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_buildings_overlay(area: str, overlay: dict) -> None:
    with open(overlay_path(area), "w", encoding="utf-8") as f:
        json.dump(overlay, f, indent=2)


def main():
    with World() as world:
        name = world.identify("city-buildings")
        print(f"[{name}] loading building extract for area {active_area()!r}")

        area = active_area()
        data = load_building_data()
        overlay = load_buildings_overlay(area)
        for building in data["buildings"]:
            moved = overlay.get(building["id"])
            if moved:
                if "position" in moved:
                    building["position"] = moved["position"]
                if "footprint" in moved:
                    building["footprint"] = moved["footprint"]
                if "wall_material" in moved:
                    building["wall_material"] = moved["wall_material"]
                if "height_m" in moved:
                    building["height_m"] = moved["height_m"]
                if "name" in moved:
                    building["name"] = moved["name"]
        real_height_count = sum(1 for b in data["buildings"] if b["height_source"] == "tag")
        print(
            f"[{name}] {data['area_description']} — {len(data['buildings'])} buildings "
            f"({real_height_count} with a real OSM height tag, "
            f"{len(data['buildings']) - real_height_count} estimated) "
            f"({data['attribution']}, {data['license']})"
        )

        control = world.create_entity()
        world.set_attribute(control, "building_attribution", data["attribution"])
        world.set_attribute(control, "building_area_description", data["area_description"])

        building_by_entity = {}

        for building in data["buildings"]:
            entity = world.create_entity()
            building_by_entity[entity] = building
            world.set_attribute(entity, "kind", "building")
            world.set_attribute(entity, "building_osm_id", building["id"])
            world.set_attribute(entity, "building_footprint", building["footprint"])
            world.set_attribute(
                entity, "building_position",
                {"x": building["position"]["x"], "y": 0.0, "z": building["position"]["z"]},
            )
            world.set_attribute(entity, "building_height_m", building["height_m"])
            world.set_attribute(entity, "building_height_source", building["height_source"])
            if building.get("wall_material"):
                world.set_attribute(entity, "building_material", building["wall_material"])
            if building.get("building_type"):
                world.set_attribute(entity, "building_type", building["building_type"])
            if building.get("name"):
                world.set_attribute(entity, "building_name", building["name"])

            # No real OSM building:material tag — ask the (optional,
            # may-not-be-running) ai-test spin to infer a plausible wall
            # material from the real signal we do have (type/height/
            # footprint size). ai-test is domain-agnostic and doesn't
            # know "building" exists; we do the building-specific
            # prompt-shaping and response-handling here, per
            # [[project_universal_ai_spin_design]]'s "calling spin shapes
            # the prompt" rule.
            if not building.get("wall_material"):
                world.set_attribute(entity, "ai_request", {
                    "prompt": (
                        "You are classifying the primary exterior wall material of a "
                        "real building for a city-simulation renderer. Reply with ONLY "
                        "one word from this list: brick, concrete, stucco, wood, glass, "
                        "stone, metal. Base your answer on the building type, height, and "
                        "footprint size given in the context."
                    ),
                    "context": {
                        "building_type": building.get("building_type"),
                        "height_m": building["height_m"],
                        "height_source": building["height_source"],
                        "footprint": building["footprint"],
                    },
                    "request_id": f"wall-material-{entity}",
                    "task_hint": "simple",
                })

        def handle_ai_material_response(entity, attribute, value, source):
            if not isinstance(value, dict):
                return
            request_id = value.get("request_id")
            if not isinstance(request_id, str) or not request_id.startswith("wall-material-"):
                return
            text = value.get("text")
            if not text:
                return
            material = text.strip().split()[0].strip(".,").lower()
            world.set_attribute(entity, "building_material", material)
            world.set_attribute(entity, "building_material_source", "ai")

        world.subscribe("ai_response", handle_ai_material_response, replay=True)

        # Sandbox mode: rendering-test-ui writes this on a mouse drag-drop
        # (desktop only for now — see the design memory this was built
        # from, project_city_spin_sandbox_road_regen_design). The UI only
        # shapes the request (new_x/new_z); this handler does the actual
        # work and owns persistence, same division of labor as
        # ai_request/ai_response. Rigid-translation move only, no
        # rotation — footprint shape/orientation is preserved as-is.
        def handle_move_request(entity, attribute, value, source):
            if not isinstance(value, dict):
                return
            building = building_by_entity.get(entity)
            if building is None:
                return
            new_x, new_z = value.get("new_x"), value.get("new_z")
            if not isinstance(new_x, (int, float)) or not isinstance(new_z, (int, float)):
                return
            old_pos = building["position"]
            dx, dz = new_x - old_pos["x"], new_z - old_pos["z"]
            new_position = {"x": new_x, "z": new_z}
            new_footprint = [{"x": p["x"] + dx, "z": p["z"] + dz} for p in building["footprint"]]
            building["position"] = new_position
            building["footprint"] = new_footprint
            world.set_attribute(entity, "building_position", {"x": new_x, "y": 0.0, "z": new_z})
            world.set_attribute(entity, "building_footprint", new_footprint)
            # Merge into whatever's already there, don't overwrite the
            # whole record -- a building that was previously voice-edited
            # (material/height/name, see handle_edit_request below) and
            # is now ALSO being dragged must keep both sets of changes.
            record = overlay.setdefault(building["id"], {})
            record["position"] = new_position
            record["footprint"] = new_footprint
            save_buildings_overlay(area, overlay)
            print(f"[{name}] building {building['id']} (entity {entity}) moved to "
                  f"({new_x:.1f}, {new_z:.1f}), persisted to overlay")

        world.subscribe("building_move_request", handle_move_request, replay=True)

        # Voice/AI-driven single-building editing (inspect mode): the
        # calling UI never writes building_material/building_height_m/
        # building_name directly from an AI's suggested edit -- it
        # proposes a field+value here instead, and THIS handler is the
        # only thing that actually validates and applies it. This matters
        # because an LLM's JSON output is untrusted input: it can
        # hallucinate a field name or an out-of-range value, and this is
        # the one place in the codebase where AI output would otherwise
        # drive a real attribute write with no server-side check at all.
        # See project_city_spin_voice_building_edit_design memory for the
        # full reasoning.
        def handle_edit_request(entity, attribute, value, source):
            if not isinstance(value, dict):
                return
            building = building_by_entity.get(entity)
            if building is None:
                return
            request_id = value.get("request_id")
            field = value.get("field")
            spec = ALLOWED_EDIT_FIELDS.get(field) if isinstance(field, str) else None
            if spec is None:
                world.set_attribute(entity, "building_edit_result", {
                    "request_id": request_id, "ok": False,
                    "message": f"{field!r} isn't an editable building property yet.",
                })
                return
            ok, result = spec["validate"](value.get("value"))
            if not ok:
                world.set_attribute(entity, "building_edit_result", {
                    "request_id": request_id, "ok": False, "message": result,
                })
                return
            overlay_key = spec["overlay_key"]
            building[overlay_key] = result
            world.set_attribute(entity, field, result)
            if field == "building_material":
                world.set_attribute(entity, "building_material_source", "voice_edit")
            record = overlay.setdefault(building["id"], {})
            record[overlay_key] = result
            save_buildings_overlay(area, overlay)
            world.set_attribute(entity, "building_edit_result", {
                "request_id": request_id, "ok": True,
                "message": f"Updated {field} to {result!r}.",
            })
            print(f"[{name}] building {building['id']} (entity {entity}) {field} -> "
                  f"{result!r} via voice edit, persisted")

        world.subscribe("building_edit_request", handle_edit_request, replay=True)

        print(f"[{name}] published — idle (this is a static data source, nothing to tick). Ctrl+C to stop")
        try:
            # A pure data-source plugin: publish once, then just stay
            # connected (a subscriber joining later still gets the
            # replayed values) — no per-tick work exists to do.
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
