"""Rendering spin — FIRST VERTICAL SLICE only: a real, working
spin-ui:// renderer that subscribes to physics-test's synthetic scene
(position/orientation/physics_lod_tier per entity, plus shape/radius/
half_extents) and draws it live in a sandboxed three.js iframe, with one
real adaptive-fidelity mechanism (full geometry for "full" tier bodies,
cheap impostors for "kinematic" tier bodies — see rendering-test-ui/src/
SceneView.tsx). Also subscribes to city-roads/city-buildings' real
OpenStreetMap Klosterneuburg extract (road_network, building_footprint,
building_height_m) and draws real road lines plus basic extruded building
footprints — not photorealistic, that's the later Cesium/3D-Tiles piece.
This is NOT the eventual GTA6-style adaptive renderer — no ray tracing,
no temporal denoising, no real-world 3D-Tiles data — just the proven
first slice of the pattern those build on later.

This process's own job is small and exactly mirrors graphing.py's own
shape: own ONE control Entity, publish this spin's OWN settings
("render_settings": quality_preset + compute_device) so they're
Core-backed and discoverable the same replayed-subscribe way any other
plugin's control Entity is (graphing.py's "graphs", city_residents.py's
"avoid_rain"), and log external edits. It does NOT create any of the
rendered scene's entities itself, and has no idea whether physics-test
is even running — same ignorance-of-the-producer relationship
agent_behavior_test.py already has with physics-test's own attributes,
just for reading instead of writing. All the actual rendering happens in
rendering-test-ui's own three.js content, never in this process.

Registered in plugins/registry.json as "rendering-test" — start it from
the Toolbox alongside physics-test, like any other plugin (see
feedback_dogfood_the_ui discipline).
"""

import time
from typing import Any

from sotrice_client import World

DEFAULT_RENDER_SETTINGS: dict[str, Any] = {
    # "low" | "medium" | "high" -- controls how aggressively
    # rendering-test-ui reduces detail per LOD tier (segment counts,
    # whether a "kinematic"-tier body gets a shaded impostor or a flat
    # unlit one). This does NOT change physics-test's own
    # FULL_SIM_RADIUS/KINEMATIC_RADIUS activation ranges -- this process
    # never writes to physics-test's files or attributes, so the actual
    # tier a body is classified into is untouched; only how each tier
    # gets DRAWN changes.
    "quality_preset": "medium",
    # "auto" | a real DXGI adapter name (see the settings panel's device
    # picker, backed by a real Tauri-side DXGI enumeration) | "remote:<address>"
    # for the not-yet-implemented remote-compute stub. Stored here purely
    # as a preference this control Entity remembers -- selecting a real
    # discrete/integrated adapter nudges three.js's own WebGLRenderer
    # powerPreference hint (see SceneView.tsx), which is the one real,
    # if crude, effect it has today.
    "compute_device": "auto",
}


def main() -> None:
    with World() as world:
        name = world.identify("rendering-test")
        print(f"[{name}] started", flush=True)

        control = world.create_entity()
        world.set_attribute(control, "kind", "rendering_control")
        # Published LAST, deliberately -- see graphing.py's identical
        # ordering note: a replay-subscriber that only cares about
        # "render_settings" never has to think about attribute
        # ordering, it just waits for the one push it asked for.
        world.set_attribute(control, "render_settings", DEFAULT_RENDER_SETTINGS)
        print(f"[{name}] control entity {control}, settings={DEFAULT_RENDER_SETTINGS}", flush=True)

        def on_settings_changed(entity: int, _name: str, value: Any, source: str | None) -> None:
            if source == name:
                return
            print(f"[{name}] render_settings updated externally (entity {entity}, source {source}): {value}", flush=True)

        world.subscribe("render_settings", on_settings_changed, replay=False)

        while True:
            time.sleep(1.0)


if __name__ == "__main__":
    main()
