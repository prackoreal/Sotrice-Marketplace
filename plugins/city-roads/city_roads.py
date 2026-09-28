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


def main():
    with World() as world:
        name = world.identify("city-roads")
        print(f"[{name}] loading road extract for area {active_area()!r}")

        data = load_road_data()
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
