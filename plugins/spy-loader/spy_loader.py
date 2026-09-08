"""Spy Loader — a normal, persistent plugin (drag it onto the canvas from
the Toolbox like anything else) that loads .spy scene files on request and
publishes them into the Core using the EXACT SAME attribute vocabulary
city_layout.py already uses. That's the whole point: city_weather.py,
city_routing.py, and city_residents.py never get touched, and never need
to be — they already only care about attribute NAMES (kind, x, y,
building_type, nearest_intersection, waypoints, ...), never about who
wrote them or how. A .spy-imported building is indistinguishable, to
every other plugin, from one city_layout.py generated procedurally.

Deliberately NOT a one-shot "read argv[1] and idle" script — "which file"
is a runtime choice, not a startup argument, because plugging this in is
a separate, visual action (drag it onto the canvas) from picking a file
to load (done afterward, from its own canvas block, or the Files panel).
So instead of a CLI arg, this plugin creates its own control Entity
(kind="spy_loader_control") and subscribes to "spy_load_path" on it —
the UI writes a staged file's path there whenever you pick one, the
same "a control Entity is just an Attribute the UI writes and the
plugin reacts to" pattern city_weather.py's city_time_speed and
demo_moving_entities.py's sim_paused already use. Loading is additive —
each path that arrives publishes MORE entities without touching earlier
ones, so one running instance can load several scenes over its life;
unplugging it (the Core's normal despawn-by-source) removes everything
it ever loaded, in one action, exactly like unplugging any other plugin.

A .spy file can't know real Entity ids ahead of time (those are assigned
fresh by the Core every run) — so objects reference each other by their
OWN local string "id" field (e.g. a building's "nearest_intersection"
names another object's "id" in the same file), resolved to real Entity
ids at load time, the same shape city_layout.py's own main() already
uses (building a list of intersection Entities first, then referencing
them by index for roads/buildings).

.spy v1 schema:

    {
      "format": "spy", "version": 1,
      "objects": [
        {"id": "isect-A", "kind": "intersection", "position": [x, y]},
        {"id": "road-AB", "kind": "road", "from": "isect-A", "to": "isect-B",
         "waypoints": [[x, y], ...]},
        {"id": "house-1", "kind": "building", "building_type": "house",
         "position": [x, y], "nearest_intersection": "isect-A",
         "geometry": "box", "color": "#3fb950"}
      ]
    }

"geometry"/"color" are the one deliberately NEW, deliberately namespaced
("render.geometry"/"render.color") addition — a rendering hint, not a
city-domain concept, kept separate on purpose so a future non-city .spy
scene wouldn't inherit anything building-shaped just to say what color
it is. Everything else reuses city_layout.py's existing bare vocabulary
on purpose, since sharing a vocabulary within one domain (both this
plugin and city_layout.py are "Layout") is the opposite of the problem
namespacing solves — that's for keeping UNRELATED domains apart.
"""

import json
import time

from city_layout import polyline_length
from sotrice_client import World


def load_scene(path):
    with open(path, "r", encoding="utf-8") as f:
        scene = json.load(f)
    if scene.get("format") != "spy":
        raise ValueError(f"not a recognized .spy scene (expected \"format\": \"spy\"): {path}")
    return scene.get("objects", [])


def publish_scene(world, name, scene_path):
    try:
        objects = load_scene(scene_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[{name}] failed to load {scene_path}: {exc}", flush=True)
        return

    entity_by_id = {}
    for obj in objects:
        entity_by_id[obj["id"]] = world.create_entity()

    updates = []
    skipped = 0
    for obj in objects:
        entity = entity_by_id[obj["id"]]
        kind = obj.get("kind")
        updates.append((entity, "kind", kind))

        if kind == "intersection":
            x, y = obj["position"]
            updates.append((entity, "x", x))
            updates.append((entity, "y", y))

        elif kind == "road":
            waypoints = obj["waypoints"]
            updates.append((entity, "from_intersection", entity_by_id[obj["from"]]))
            updates.append((entity, "to_intersection", entity_by_id[obj["to"]]))
            updates.append((entity, "waypoints", waypoints))
            updates.append((entity, "length", round(polyline_length(waypoints), 1)))

        elif kind == "building":
            x, y = obj["position"]
            building_type = obj["building_type"]
            updates.append((entity, "building_type", building_type))
            updates.append((entity, "x", x))
            updates.append((entity, "y", y))
            nearest_id = obj.get("nearest_intersection")
            if nearest_id is not None:
                updates.append((entity, "nearest_intersection", entity_by_id[nearest_id]))
            if building_type in ("bakery", "restaurant"):
                updates.append((entity, "revenue", 0))
                updates.append((entity, "customers_served", 0))

        else:
            print(f"[{name}] object '{obj.get('id')}' has unrecognized kind {kind!r}, skipping its fields (still gets an Entity)", flush=True)
            skipped += 1

        # Rendering hints — deliberately namespaced, deliberately separate
        # from the city-domain fields above (see module docstring): any
        # object may carry these regardless of kind.
        if "geometry" in obj:
            updates.append((entity, "render.geometry", obj["geometry"]))
        if "color" in obj:
            updates.append((entity, "render.color", obj["color"]))

    world.set_many(updates)
    print(f"[{name}] published {len(objects)} entities ({skipped} with unrecognized kind) from {scene_path}", flush=True)


def main():
    with World() as world:
        name = world.identify("spy-loader")
        control = world.create_entity()
        # The one thing the UI needs to find THIS instance's control
        # Entity: "kind" is already in every client's generic wildcard
        # watch (see app.html's simEntities), so no new discovery
        # mechanism is needed beyond a value nothing else uses.
        world.set_attribute(control, "kind", "spy_loader_control")
        print(f"[{name}] ready — waiting for a file to be picked from its canvas block or the Files panel", flush=True)

        def on_load_path(entity, attribute, value, source):
            if entity != control or not value:
                return
            publish_scene(world, name, value)

        world.subscribe("spy_load_path", on_load_path, replay=False)

        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
