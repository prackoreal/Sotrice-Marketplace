"""The "graphing" plugin: a standalone 2D/3D function-plotting surface,
following the exact same shape as any other Sotrice plugin (city-weather's
clock, city-residents' avoid_rain toggle) — it owns one control Entity and
publishes/accepts everything through plain Attributes, never a bespoke API.

What this plugin is NOT: it is not math-teacher-specific, and it does not
import or depend on anything under math_teacher.py/math_student.py. It is
meant to be usable standalone (drag it onto the canvas and get a graphing
calculator) AND embeddable by any other plugin that wants to show a graph,
by writing to this plugin's "graphs" attribute the same way city_routing.py
writes "route" for city_residents.py to read — see clients/python/README.md's
"control Entity" pattern, and clients/python/graphing-ui/DESIGN.md for the
full design rationale (why a standalone plugin, why this exact data shape,
why these rendering libraries).

Data model (the ONE thing this plugin actually owns): a single Attribute,
"graphs", holding a JSON array of graph descriptors:

    {
      "id": "f1",                    # stable key, so an update replaces
                                      # rather than duplicates
      "kind": "function2d",          # "function2d" | "surface3d" -- both
                                      # are rendered end-to-end now
                                      # (function-plot / three.js -- see
                                      # DESIGN.md's library section)
      "expr": "sin(x) + x^2/12",     # a plain math expression string,
                                      # evaluated by the UI (mathjs), never
                                      # by this Python process — this
                                      # process only stores and relays it
      "domain": [-10, 10],           # [min, max] for function2d's x axis;
                                      # for surface3d this is domain_x, and
                                      # a second "domain_y" key applies
      "color": "#4f7cff",
      "label": "sin(x) + x^2/12",
      "visible": true
    }

Any plugin (or this plugin's own UI) can rewrite "graphs" wholesale to
change what's plotted — same pattern as city_residents.py rewriting its
own "route" request wholesale rather than patching one field at a time.
This process never evaluates or validates the expression string itself
(no math library dependency here at all) — that is entirely the UI's job,
exactly as the design doc requires ("the Python side never renders,
the UI side never owns the data").
"""

import json
import time
from typing import Any

from sotrice_client import World

DEFAULT_GRAPHS: list[dict[str, Any]] = [
    {
        "id": "f1",
        "kind": "function2d",
        "expr": "sin(x) + x^2 / 12",
        "domain": [-10, 10],
        "color": "#4f7cff",
        "label": "sin(x) + x^2/12",
        "visible": True,
    },
    {
        "id": "s1",
        "kind": "surface3d",
        "expr": "sin(sqrt(x^2 + y^2))",
        "domain": [-6, 6],
        "domain_y": [-6, 6],
        "color": "#4f7cff",
        "label": "sin(sqrt(x^2+y^2))",
        "visible": True,
    },
]


def main() -> None:
    with World() as world:
        name = world.identify("graphing")
        print(f"[{name}] started", flush=True)

        control = world.create_entity()
        world.set_attribute(control, "kind", "graphing_control")

        # Published LAST, deliberately — this is the bare, non-namespaced
        # attribute a consumer (this plugin's own UI, or a different
        # plugin entirely) discovers this control Entity through, via a
        # replayed subscribe (see math-teacher-ui's identical class_code
        # discovery). Publishing "kind" first and "graphs" second means a
        # replay-subscriber that only cares about "graphs" never has to
        # care about ordering at all — it just waits for the one push it
        # asked for.
        world.set_attribute(control, "graphs", DEFAULT_GRAPHS)
        print(f"[{name}] control entity {control}, seeded {len(DEFAULT_GRAPHS)} graph(s)", flush=True)

        # React to external edits (this plugin's own UI writing back after
        # someone edits an expression, or another plugin driving this one
        # programmatically) — filtering out this connection's OWN echoed
        # write is the same source-comparison every control-Entity owner
        # in this codebase already does (see city_residents.py's
        # on_avoid_rain_toggle). Nothing downstream actually depends on
        # this handler firing (the UI reads "graphs" directly off the wire
        # itself, same connection or not) — it exists so this process's
        # own log is a real, truthful record of what the graph set is at
        # any moment, useful for exactly the kind of live verification
        # this plugin was built and checked against.
        def on_graphs_changed(entity: int, _name: str, value: Any, source: str | None) -> None:
            if source == name:
                return
            count = len(value) if isinstance(value, list) else 0
            print(f"[{name}] graphs updated externally (entity {entity}, source {source}): {count} graph(s)", flush=True)

        world.subscribe("graphs", on_graphs_changed, replay=False)

        while True:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
