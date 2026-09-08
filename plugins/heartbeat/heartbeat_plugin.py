"""A test fixture, not a real Sotrice plugin: something small and
long-running for the Launcher to actually start and stop, since the real
toy-simulation plugins (plugins/toy-simulation/BRIEF.md) don't exist yet.
Identifies as "heartbeat", creates one entity, gives it a fixed spot to
sit at (top-center — clear of where demo_moving_entities.py's dots orbit),
and increments a "beats" counter on it every half second until stopped.

ui/stage3/app.html's Simulation View treats "beats" as its own small
convention, same spirit as "active" for hiding an entity: any entity that
has it gets drawn as a heart + its count instead of a plain dot — a
plugin doesn't get a custom visual by asking the Core for one, it gets
one because the viewer happens to recognize an attribute name it agreed
on. Nothing here is Core or server concept, purely a UI-side convention.
"""

import re
import time

from sotrice_client import World

# Two instances are a real, intended scenario (see next_instance_name in
# server/src/main.rs) — a fixed (400, 80) made every instance's heart
# stack exactly on top of every other one, indistinguishable in the
# Simulation View even though each is tracked correctly underneath.
# The launch id (now this connection's own instance name — see
# sotrice_client.py's identify()) always ends in a launch number, so
# that number lays instances out in a grid instead of colliding.
GRID_COLUMNS = 6
SPACING_X = 90
SPACING_Y = 90


def grid_position(instance_name: str) -> tuple[int, int]:
    match = re.search(r"(\d+)$", instance_name)
    index = int(match.group(1)) - 1 if match else 0
    row, col = divmod(index, GRID_COLUMNS)
    return (100 + col * SPACING_X, 80 + row * SPACING_Y)


with World() as world:
    name = world.identify("heartbeat")
    print(f"[{name}] started, beating every 0.5s", flush=True)

    entity = world.create_entity()
    x, y = grid_position(name)
    world.set_attribute(entity, "x", x)
    world.set_attribute(entity, "y", y)

    beats = 0
    while True:
        beats += 1
        world.set_attribute(entity, "beats", beats)
        print(f"[{name}] beat {beats}", flush=True)
        time.sleep(0.5)
