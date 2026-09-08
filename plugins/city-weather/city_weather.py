"""City Weather — deliberately simple: ONE rain cloud that shows up
occasionally somewhere in the city, sits for a while, then clears —
instead of several permanent zones cycling through conditions at once.
The earlier version (6 zones, 4 conditions, all flipping independently)
was accurate to "exaggerated local weather" but unreadable — you
couldn't tell what was actually happening at a glance. One cloud you
can watch appear, sit somewhere, and vanish is the legible version of
the same idea; Routing's existing weather penalty already makes
residents visibly detour around it once it's there.

Where the rain actually lands isn't random-anywhere: this plugin learns
the road graph through the Core (subscribing to the same "kind"/
"waypoints"/etc. attributes city-routing does — never touching
city-layout directly) and, when picking a spot to rain, prefers a road
that's one of several PARALLEL options between the same two
intersections — i.e. the 3-lane split city-layout builds. That's what
makes "does traffic avoid the rainy lane" an actually visible, testable
question instead of a coin flip on whether the rain even landed
somewhere that mattered.

Also owns the shared "what time is it" clock the other city plugins key
their behavior off of (morning bakery rush, evening restaurant rush) —
time and weather are naturally coupled in real life, so bundling the
clock here avoids inventing a fifth plugin just to tick a number.

Entities published:
  A single control entity:
    city_hour (0-24, wraps) — advances by city_time_speed minutes every
      tick. Also externally settable: something else (the UI) can jump
      the clock directly by setting city_hour itself — this plugin
      notices via its own subscription and adopts that as the new time
      rather than fighting it on the next tick.
    city_time_speed (minutes of simulated time advanced per tick) —
      externally settable the same way, so the UI can speed up, slow
      down, or effectively pause the clock without touching this file.

  A single weather-zone entity, always present, kind="weather_zone":
  x, y, radius, condition ("clear"|"rain"), severity (0 when clear, so
  Routing's penalty is a no-op; >0 while raining). x/y/radius only mean
  anything while condition is "rain" — the renderer should just not
  draw anything for "clear".
"""

import random
import time
from collections import defaultdict

from city_layout import point_along_road
from sotrice_client import World

WORLD_WIDTH = 1600
WORLD_HEIGHT = 1200
SEVERITY_BY_CONDITION = {"clear": 0.0, "rain": 0.8}

DEFAULT_MINUTES_PER_TICK = 3  # simulated minutes per tick at 1x speed — "it should go by the minute"
TICK_SECONDS = 1.0
RAIN_START_CHANCE = 0.03    # per tick, while clear — "occasionally", not constantly
RAIN_DURATION_TICKS = (15, 30)
# Tight on purpose: the 3 parallel lanes are only ~140px apart at their
# bow, so a wide radius would blanket all three and there'd be nothing
# to actually avoid. Sized to cover one lane convincingly without
# reliably reaching its neighbors.
RAIN_RADIUS = (90, 120)

minutes_per_tick = DEFAULT_MINUTES_PER_TICK


with World() as world:
    name = world.identify("city-weather")
    print(f"[{name}] one rain cloud, occasional, ticking the city clock", flush=True)

    control = world.create_entity()
    hour = random.uniform(0, 24)
    world.set_attribute(control, "city_hour", round(hour, 4))
    world.set_attribute(control, "city_time_speed", minutes_per_tick)

    def on_city_hour_external(entity, attribute, value, source):
        # Ignore our own pushes echoing back — only adopt a time set by
        # someone else (the UI's "set the clock" control).
        global hour
        if source == name:
            return
        hour = value

    def on_time_speed_external(entity, attribute, value, source):
        global minutes_per_tick
        if source == name:
            return
        minutes_per_tick = value

    world.subscribe("city_hour", on_city_hour_external, replay=False)
    world.subscribe("city_time_speed", on_time_speed_external, replay=False)

    # Learning the road graph the same way city-routing does (see that
    # file) — purely through the Core, no import of/reference to
    # city-layout's own data structures. Only used to pick a rain
    # target, never to route anything.
    roads = {}  # entity -> {"from", "to", "waypoints"}
    fields = defaultdict(dict)

    def remember_road(entity, attribute, value, source):
        fields[entity][attribute] = value
        record = fields[entity]
        if record.get("kind") == "road" and all(
            k in record for k in ("from_intersection", "to_intersection", "waypoints")
        ):
            roads[entity] = {
                "from": record["from_intersection"],
                "to": record["to_intersection"],
                "waypoints": record["waypoints"],
            }

    for attribute in ("kind", "from_intersection", "to_intersection", "waypoints"):
        world.subscribe(attribute, remember_road, replay=True)

    def pick_rain_target():
        """A road that's one of several parallel options between the
        same two intersections — the 3-lane split, once city-layout has
        published it. Falls back to any known road (or None, if nothing
        is known yet at all) so this never blocks weather from working
        against a differently-shaped layout in the future."""
        groups = defaultdict(list)
        for road_entity, road in roads.items():
            key = tuple(sorted((road["from"], road["to"])))
            groups[key].append(road_entity)
        parallel_groups = [g for g in groups.values() if len(g) > 1]
        if parallel_groups:
            return roads[random.choice(random.choice(parallel_groups))]
        if roads:
            return random.choice(list(roads.values()))
        return None

    zone = world.create_entity()
    condition = "clear"
    rain_ticks_left = 0
    world.set_many([
        (zone, "kind", "weather_zone"),
        (zone, "x", 0.0),
        (zone, "y", 0.0),
        (zone, "radius", 0.0),
        (zone, "condition", condition),
        (zone, "severity", SEVERITY_BY_CONDITION[condition]),
    ])

    try:
        while True:
            hour = (hour + minutes_per_tick / 60) % 24
            world.set_attribute(control, "city_hour", round(hour, 4))

            if condition == "clear":
                if random.random() < RAIN_START_CHANCE:
                    target = pick_rain_target()
                    if target is not None:
                        rx, ry, _, _ = point_along_road(target["waypoints"], 0.5)
                    else:
                        rx = random.uniform(150, WORLD_WIDTH - 150)
                        ry = random.uniform(150, WORLD_HEIGHT - 150)
                    condition = "rain"
                    rain_ticks_left = random.randint(*RAIN_DURATION_TICKS)
                    world.set_many([
                        (zone, "x", round(rx, 1)),
                        (zone, "y", round(ry, 1)),
                        (zone, "radius", round(random.uniform(*RAIN_RADIUS), 1)),
                        (zone, "condition", condition),
                        (zone, "severity", SEVERITY_BY_CONDITION[condition]),
                    ])
            else:
                rain_ticks_left -= 1
                if rain_ticks_left <= 0:
                    condition = "clear"
                    world.set_attribute(zone, "condition", condition)
                    world.set_attribute(zone, "severity", SEVERITY_BY_CONDITION[condition])

            time.sleep(TICK_SECONDS)
    except KeyboardInterrupt:
        print(f"[{name}] stopped")
