"""city-roads — a real, minimal road-network DATA SOURCE spin. Loads a
real OpenStreetMap extract (a few real city blocks around central
Klosterneuburg, Austria — see `scripts/extract_city_roads.py` for how it
was produced and `clients/python/city_roads_data/klosterneuburg_center.json`
for the data itself) and publishes the routable node/edge graph as
ordinary attributes, for anything else to consume — agent-behavior-test
uses it today to replace "wander to a random point" with real pathfinding
along real roads, but this file has no idea that's who's listening, the
same ignorance-of-the-consumer this project's other watch/write plugins
already have.

This is explicitly the road-NETWORK/routing-data problem, not the
photorealistic-building/3D-Tiles problem — no building geometry, no
textures, just a routable graph. A "google 3d tiles spin" feeding real
building geometry is a separate, later, unrelated piece of work.

No live API calls at runtime — the extract is a static, versioned data
file checked into the repo (same "no live API" discipline this project's
LaMa integration already uses for its own licensed content), re-generated
by re-running `scripts/extract_city_roads.py` if the source area needs to
change, not by this plugin reaching out to Overpass itself every launch.

Data license: OpenStreetMap data is © OpenStreetMap contributors,
licensed ODbL (https://www.openstreetmap.org/copyright) — real
attribution is required wherever this data is displayed or shipped. This
plugin republishes the attribution string as its own bare
`road_attribution` attribute specifically so any UI/consumer can show it
without having to know the data's own provenance file format.

Registered in plugins/registry.json as "city-roads".
"""

import json
import os
import time

import road_regen
from sotrice_client import World

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "city_roads_data")
ACTIVE_AREA_PATH = os.path.join(DATA_DIR, "active_area.json")
DEFAULT_AREA = "klosterneuburg_center"


def active_area() -> str:
    """Which area's extract to load — a small, editable config file
    (`city_roads_data/active_area.json`), not a code change, so switching
    areas (or agent-behavior-test's per-area agent count, see that file)
    doesn't need touching this plugin at all. Missing entirely, or
    missing the `area` key, falls back to this project's original single
    extract — nothing that already depends on today's behavior changes
    unless that file is actually edited."""
    if os.path.exists(ACTIVE_AREA_PATH):
        with open(ACTIVE_AREA_PATH, encoding="utf-8") as f:
            return json.load(f).get("area", DEFAULT_AREA)
    return DEFAULT_AREA


def load_road_data():
    data_path = os.path.join(DATA_DIR, f"{active_area()}.json")
    with open(data_path, encoding="utf-8") as f:
        return json.load(f)


def roads_overlay_path(area: str) -> str:
    return os.path.join(DATA_DIR, f"{area}_roads_overlay.json")


def load_roads_overlay(area: str) -> dict:
    """A sequence of sandbox-mode local road regenerations, each
    triggered by a building move (see road_regen.py and the design
    memory this was built from). Replayed in order on top of the real
    OSM extract at load time — the extract file itself is never mutated
    (keeps the real ODbL data honest/untouched); this is Sotrice's own
    generated geometry layered on top."""
    path = roads_overlay_path(area)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"regenerations": []}


def save_roads_overlay(area: str, overlay: dict) -> None:
    with open(roads_overlay_path(area), "w", encoding="utf-8") as f:
        json.dump(overlay, f, indent=2)


def main():
    with World() as world:
        name = world.identify("city-roads")
        print(f"[{name}] loading road extract for area {active_area()!r}")

        area = active_area()
        data = load_road_data()
        overlay = load_roads_overlay(area)
        for record in overlay["regenerations"]:
            road_regen.apply_regeneration(data["nodes"], data["edges"], record)
        if overlay["regenerations"]:
            print(f"[{name}] replayed {len(overlay['regenerations'])} persisted sandbox "
                  f"road regeneration(s) from a previous session")
        print(
            f"[{name}] {data['area_description']} — "
            f"{len(data['nodes'])} nodes, {len(data['edges'])} directed edges "
            f"({data['attribution']}, {data['license']})"
        )

        control = world.create_entity()
        # One bundled JSON blob is enough for a graph this size (tens of
        # KB) — no per-node entity needed, matching the brief's own "not
        # the enumeration problem" framing: a consumer just reads one
        # attribute and has the whole routable graph, nodes keyed by
        # their real OSM id (as a string, since JSON object keys are
        # always strings) with local x/z meters plus the original
        # lat/lon, and edges as (from, to, length_m, name, highway)
        # tuples — already directed, so a consumer never needs to
        # re-derive oneway handling itself.
        world.set_attribute(control, "road_network", {"nodes": data["nodes"], "edges": data["edges"]})
        world.set_attribute(control, "road_attribution", data["attribution"])
        world.set_attribute(control, "road_area_description", data["area_description"])

        # Sandbox mode: a real, NEW cross-plugin dependency on
        # city-buildings (this file used to subscribe to nothing at
        # all). When a building's footprint genuinely CHANGES (a drag
        # move in rendering-test-ui, relayed via city_buildings.py), the
        # local road network around it is regenerated so roads reconnect
        # sensibly instead of cutting through the building's new spot.
        # See road_regen.py for the actual geometry and the design
        # memory (project_city_spin_sandbox_road_regen_design) for why
        # it's scoped the way it is.
        #
        # last_footprint_by_entity distinguishes "first time this
        # entity's footprint is seen" (either a pristine building, or —
        # at our own startup — a building whose overlay-adjusted
        # position already reflects a PAST move that this file's own
        # roads overlay already regenerated for) from a genuine LIVE
        # move during this run. Only the latter triggers regeneration —
        # this makes replay-on-subscribe and restart both naturally
        # idempotent with no extra bookkeeping needed.
        last_footprint_by_entity = {}

        def handle_building_footprint(entity, attribute, value, source):
            if not isinstance(value, list):
                return
            points = [(p["x"], p["z"]) for p in value]
            previous = last_footprint_by_entity.get(entity)
            last_footprint_by_entity[entity] = points
            if previous is None or previous == points:
                return

            record = road_regen.regenerate_around_building(
                data["nodes"], data["edges"], points, f"e{entity}",
            )
            road_regen.apply_regeneration(data["nodes"], data["edges"], record)
            overlay["regenerations"].append(record)
            save_roads_overlay(area, overlay)
            world.set_attribute(control, "road_network", {"nodes": data["nodes"], "edges": data["edges"]})
            print(f"[{name}] regenerated local roads around moved building (entity {entity}): "
                  f"-{len(record['removed_node_ids'])} node(s), "
                  f"+{len(record['added_nodes'])} synthetic bypass node(s)")

        world.subscribe("building_footprint", handle_building_footprint, replay=True)

        print(f"[{name}] published — idle (this is a static data source, nothing to tick). Ctrl+C to stop")
        try:
            # A pure data-source plugin: publish once, then just stay
            # connected (a subscriber joining later still gets the
            # replayed value) — no per-tick work exists to do.
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
