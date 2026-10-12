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


def main():
    with World() as world:
        name = world.identify("city-buildings")
        print(f"[{name}] loading building extract for area {active_area()!r}")

        data = load_building_data()
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

        for building in data["buildings"]:
            entity = world.create_entity()
            world.set_attribute(entity, "kind", "building")
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
