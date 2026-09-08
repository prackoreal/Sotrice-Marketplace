"""A more complex test fixture than heartbeat or moving-entities: THREE
sensor entities, each independently drifting THREE attributes
(temperature, humidity, wind_speed) at once, all from one plugin
instance. Not a real Sotrice plugin — built specifically to exercise,
in one process, things the simpler fixtures couldn't:

- Mindmap View juggling several (source, attribute) variables from one
  instance at once, and staying in sync as all of them change together.
- Whole-plugin enable/disable (DisablePlugin/EnablePlugin in
  server/src/main.rs) actually has to freeze NINE separate attributes
  at once, not just heartbeat's single "beats" counter.
- Despawn-on-unplug has to clean up THREE entities from one launch, not
  just one.

Each sensor also gets an x/y so it shows as a plain dot in Simulation
View too (no "beats" attribute, so the viewer's existing renderDot
convention applies unchanged).
"""

import random
import time

from sotrice_client import World

SENSOR_COUNT = 3


def drift(value: float, step: float, low: float, high: float) -> float:
    value += random.uniform(-step, step)
    return max(low, min(high, value))


with World() as world:
    name = world.identify("weather-station")
    print(f"[{name}] started, {SENSOR_COUNT} sensors drifting", flush=True)

    sensors = []
    for i in range(SENSOR_COUNT):
        entity = world.create_entity()
        world.set_attribute(entity, "x", 150 + i * 220)
        world.set_attribute(entity, "y", 420)
        sensors.append({
            "entity": entity,
            "temperature": 18.0 + random.uniform(-2, 2),
            "humidity": 50.0 + random.uniform(-10, 10),
            "wind_speed": 5.0 + random.uniform(-2, 2),
        })

    try:
        while True:
            for sensor in sensors:
                sensor["temperature"] = drift(sensor["temperature"], 0.4, -10, 40)
                sensor["humidity"] = drift(sensor["humidity"], 1.5, 0, 100)
                sensor["wind_speed"] = drift(sensor["wind_speed"], 0.6, 0, 30)
                world.set_attribute(sensor["entity"], "temperature", round(sensor["temperature"], 1))
                world.set_attribute(sensor["entity"], "humidity", round(sensor["humidity"], 1))
                world.set_attribute(sensor["entity"], "wind_speed", round(sensor["wind_speed"], 1))
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"[{name}] stopped")
