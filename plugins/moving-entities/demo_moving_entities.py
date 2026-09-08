"""What ui/stage3/app.html's Simulation View actually watches. Registered
in plugins/registry.json as "moving-entities" — start it from the
Toolbox in the running app, the same way any real plugin gets plugged
in, rather than running it from a terminal on the side. A script the UI
has no idea is running would show up in Simulation View anyway (the Core
doesn't care who writes what), but that defeats the entire point of a
Toolbox: what's visibly running should always trace back to something
you can see and control as plugged in.

Pause/resume and entity count are real, live-controllable: the browser
writes "sim_paused" and "sim_entity_count" onto a dedicated control
entity, and this script is the one that actually acts on them. Nothing
new needed in the Core for that — it's the same subscribe+replay
mechanism the viewer already uses to find out what exists, just used to
carry control values instead of positions.

The Core has no entity-deletion API (by design, see core/src/entity.rs),
so "reducing" the count doesn't destroy entities — it marks the excess
ones "active": false and stops updating their position. The viewer hides
anything not active. Raising the count again just re-activates them from
wherever they were left, or creates fresh ones if there aren't enough yet.
"""

import math
import threading
import time

from sotrice_client import World

MIN_ENTITIES = 1
MAX_ENTITIES = 16
GRID_COLUMNS = 4


def center_for_index(index: int) -> tuple[float, float]:
    margin_x, margin_y = 100, 130
    spacing_x = (800 - 2 * margin_x) / (GRID_COLUMNS - 1)
    spacing_y = 140
    row, col = divmod(index, GRID_COLUMNS)
    return (margin_x + col * spacing_x, margin_y + row * spacing_y)


with World() as world:
    name = world.identify("moving-entities-demo")
    print(f"[{name}] creating the control entity and starting entities")

    control = world.create_entity()
    world.set_attribute(control, "sim_paused", False)
    world.set_attribute(control, "sim_entity_count", 4)

    state_lock = threading.Lock()
    paused = False
    target_count = 4

    def on_paused(entity, attribute, value, source):
        global paused
        if entity != control:
            return
        with state_lock:
            paused = bool(value)

    def on_count(entity, attribute, value, source):
        global target_count
        if entity != control:
            return
        with state_lock:
            target_count = max(MIN_ENTITIES, min(MAX_ENTITIES, int(value)))

    world.subscribe("sim_paused", on_paused, replay=True)
    world.subscribe("sim_entity_count", on_count, replay=True)

    entities: list[int] = []
    centers: list[tuple[float, float]] = []
    angles: list[float] = []
    is_active: list[bool] = []

    def ensure_entity(index: int) -> None:
        while len(entities) <= index:
            new_index = len(entities)
            entity = world.create_entity()
            entities.append(entity)
            center = center_for_index(new_index)
            centers.append(center)
            angles.append(float(new_index))
            is_active.append(False)
            world.set_attribute(entity, "active", False)
            # A brand-new entity otherwise has no x/y at all until the
            # movement loop below runs at least once — which it never
            # does while paused, so raising the count while paused
            # created the entity but left it permanently invisible
            # (the viewer requires both x and y before it'll draw
            # anything). Placing it at its own center immediately means
            # it always has somewhere to be drawn from the instant it
            # exists, paused or not — "one cycle" to create it, exactly.
            cx, cy = center
            world.set_attribute(entity, "x", cx)
            world.set_attribute(entity, "y", cy)

    for i in range(4):
        ensure_entity(i)

    try:
        while True:
            with state_lock:
                currently_paused = paused
                wanted = target_count

            ensure_entity(wanted - 1)

            for i, entity in enumerate(entities):
                should_be_active = i < wanted
                if should_be_active != is_active[i]:
                    is_active[i] = should_be_active
                    world.set_attribute(entity, "active", should_be_active)

            if not currently_paused:
                for i, entity in enumerate(entities):
                    if not is_active[i]:
                        continue
                    angles[i] += 0.15
                    cx, cy = centers[i]
                    x = cx + 60 * math.cos(angles[i])
                    y = cy + 60 * math.sin(angles[i])
                    world.set_attribute(entity, "x", round(x, 1))
                    world.set_attribute(entity, "y", round(y, 1))

            time.sleep(0.1)
    except KeyboardInterrupt:
        print(f"[{name}] stopped")
