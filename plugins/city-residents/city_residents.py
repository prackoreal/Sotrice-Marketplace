"""City Residents — the actual agents. Reads city_layout's buildings
(to pick a home and to know what facilities exist) and city_weather's
clock (to decide WHEN to go somewhere), then asks city_routing HOW to
get there by setting "desired_destination" on its own entity and
waiting for "route" to come back — never touching city_layout's roads
or city_routing's graph directly. Each resident is a small state
machine: at home -> decide to go somewhere -> traveling -> arrived ->
wait a bit -> head back home -> repeat.

Movement and building-assignment both reuse city_layout's own
walk_polyline/point-on-path geometry rather than a second
reimplementation, so a resident interpolates its route exactly the way
city_routing built it.
"""

import random
import time
from collections import defaultdict

from city_layout import walk_polyline
from sotrice_client import World

RESIDENT_COUNT = 20   # capped at however many houses city-layout actually built (currently 12)
SPEED = 55.0          # pixels per simulated second while traveling, AT 1x speed (see speed_multiplier below)
TICK_SECONDS = 0.2
MIN_WAIT_TICKS = 15   # time spent lingering after arriving somewhere, before deciding the next move
MAX_WAIT_TICKS = 45
LEAVE_HOME_CHANCE = 0.06  # per tick, while idle at home with no wait timer running
VISIT_HOUSE_CHANCE = 0.25  # of those trips, how many are "visit another resident" instead of a facility
REVENUE_PER_VISIT = (4, 12)  # random range — a "sale" a bakery/restaurant makes per customer
# Must match city_weather.py's DEFAULT_MINUTES_PER_TICK — the speed
# dropdown's value IS "simulated minutes per tick", and residents need
# the same baseline to turn that into a speed multiplier ("the speed
# changer should influence the speed of the residents too, not only the
# time" — a 0 here also means 0 baseline movement, i.e. Paused).
BASE_MINUTES_PER_TICK = 3


def choose_destination_type(hour):
    """Which FACILITY type (never "house" — that's a separate, explicit
    "go visit someone" choice made by the caller) a resident leans
    toward at this hour. Deliberately overlapping windows (park and
    restaurant both cover part of the evening) so the transition feels
    like a gradient, not a light switch."""
    weights = {"bakery": 1, "restaurant": 1, "school": 1, "park": 1}
    if 6 <= hour < 10:
        weights["bakery"] = 6
    if 7 <= hour < 15:
        weights["school"] = 4
    if 14 <= hour < 19:
        weights["park"] = 4
    if 17 <= hour < 21:
        weights["restaurant"] = 6
    types = list(weights.keys())
    picks = list(weights.values())
    return random.choices(types, weights=picks, k=1)[0]


class Resident:
    def __init__(self, entity, home_entity, home_pos, home_intersection):
        self.entity = entity
        self.home_entity = home_entity
        self.home_pos = home_pos
        self.at_intersection = home_intersection
        self.at_home = True
        self.wait_ticks = random.randint(0, MAX_WAIT_TICKS)  # stagger everyone so they don't all move in lockstep
        self.route = None
        self.distance_traveled = 0.0
        # Requesting a route is fire-and-forget over Attribute relay, not
        # a blocking call — without this flag, a resident would keep
        # re-issuing "desired_destination" every tick until city-routing's
        # reply actually lands (which takes at least one full round trip),
        # spamming duplicate route computations for a single trip.
        self.awaiting_route = False


def main():
    buildings = {}  # entity -> {"type", "x", "y", "nearest_intersection"}
    buildings_by_type = defaultdict(list)
    city_hour = [12.0]  # mutable box so the closure below can update it

    with World() as world:
        name = world.identify("city-residents")
        print(f"[{name}] waiting on city-layout for houses and facilities", flush=True)

        fields = defaultdict(dict)

        def remember_building(entity, attribute, value, source):
            fields[entity][attribute] = value
            record = fields[entity]
            if record.get("kind") == "building" and all(
                k in record for k in ("building_type", "x", "y", "nearest_intersection")
            ):
                if entity not in buildings:
                    buildings[entity] = {
                        "type": record["building_type"],
                        "x": record["x"],
                        "y": record["y"],
                        "nearest_intersection": record["nearest_intersection"],
                        "revenue": 0,
                        "customers_served": 0,
                    }
                    buildings_by_type[record["building_type"]].append(entity)
                elif attribute in ("x", "y", "nearest_intersection"):
                    # A building can be dragged in the UI after the fact —
                    # keep this plugin's own copy of its position live
                    # instead of freezing it at first-sight, same as
                    # city-routing already does for its own building cache.
                    buildings[entity][attribute] = value

        for attribute in ("kind", "building_type", "x", "y", "nearest_intersection"):
            world.subscribe(attribute, remember_building, replay=True)

        def on_city_hour(entity, attribute, value, source):
            city_hour[0] = value

        world.subscribe("city_hour", on_city_hour, replay=True)

        # The UI's speed control is "simulated minutes per tick" (see
        # city_weather.py) — residents turn that into a plain multiplier
        # on their own walking speed, so speeding up time also speeds up
        # the people, and Paused (0) also means nobody moves.
        speed_multiplier = [1.0]

        def on_time_speed(entity, attribute, value, source):
            speed_multiplier[0] = value / BASE_MINUTES_PER_TICK if BASE_MINUTES_PER_TICK else 1.0

        world.subscribe("city_time_speed", on_time_speed, replay=True)

        # "it rains, so they switch routes" — reacting to the WEATHER
        # PLUGIN's condition, never touching its zone geometry directly
        # (that's Routing's job, via road_weather_penalty). This only
        # tracks the boolean "is it raining right now", checked once per
        # tick against its previous value so a reroute fires exactly on
        # the moment it *starts* raining, not every tick it stays rainy.
        rain_now = [False]

        def on_weather_condition(entity, attribute, value, source):
            rain_now[0] = value == "rain"

        world.subscribe("condition", on_weather_condition, replay=True)

        houses = buildings_by_type.get("house", [])
        if len(houses) < RESIDENT_COUNT:
            print(f"[{name}] only {len(houses)} houses known — waiting isn't going to help, city-layout must be started first", flush=True)
        random.shuffle(houses)

        residents = []
        for i in range(min(RESIDENT_COUNT, len(houses))):
            home_entity = houses[i]
            home = buildings[home_entity]
            entity = world.create_entity()
            resident = Resident(entity, home_entity, (home["x"], home["y"]), home["nearest_intersection"])
            residents.append(resident)

        init_updates = []
        for r in residents:
            init_updates.append((r.entity, "kind", "resident"))
            init_updates.append((r.entity, "home_building", r.home_entity))
            init_updates.append((r.entity, "at_intersection", r.at_intersection))
            init_updates.append((r.entity, "x", round(r.home_pos[0], 1)))
            init_updates.append((r.entity, "y", round(r.home_pos[1], 1)))
        world.set_many(init_updates)
        print(f"[{name}] {len(residents)} residents settled in", flush=True)

        def on_route(entity, attribute, value, source):
            for r in residents:
                if r.entity == entity:
                    r.route = value
                    r.distance_traveled = 0.0
                    r.awaiting_route = False
                    return

        world.subscribe("route", on_route, replay=False)

        was_raining = False
        try:
            while True:
                updates = []

                # Rising edge only — everyone already mid-trip re-asks
                # city-routing for a fresh path to the SAME destination,
                # now weighted by the new rain penalty. This snaps them
                # back to the intersection their CURRENT trip started
                # from (city-routing only knows "last intersection
                # reached", not "exact point along this road") rather
                # than a smooth mid-road reroute — a known simplification,
                # not a bug, and still visibly "the route changed".
                if rain_now[0] and not was_raining:
                    for r in residents:
                        if r.route is not None and not r.awaiting_route:
                            destination = r.route["destination_building"]
                            r.route = None
                            r.distance_traveled = 0.0
                            r.awaiting_route = True
                            updates.append((r.entity, "current_road", None))
                            updates.append((r.entity, "route", None))
                            updates.append((r.entity, "desired_destination", destination))
                was_raining = rain_now[0]

                for r in residents:
                    if speed_multiplier[0] == 0:
                        continue  # Paused — freeze decisions and waiting too, not just walking
                    if r.route is not None:
                        r.distance_traveled += SPEED * TICK_SECONDS * speed_multiplier[0]
                        waypoints = r.route["waypoints"]
                        x, y, segment_index = walk_polyline(waypoints, min(r.distance_traveled, r.route["length"]))
                        road_ids = r.route["segment_road_ids"]
                        current_road = road_ids[segment_index] if segment_index < len(road_ids) else None
                        updates.append((r.entity, "x", round(x, 1)))
                        updates.append((r.entity, "y", round(y, 1)))
                        updates.append((r.entity, "current_road", current_road))

                        if r.distance_traveled >= r.route["length"]:
                            destination = r.route["destination_building"]
                            r.route = None
                            r.distance_traveled = 0.0
                            r.at_home = destination == r.home_entity
                            arrived_building = buildings.get(destination)
                            if arrived_building:
                                r.at_intersection = arrived_building["nearest_intersection"]
                                updates.append((r.entity, "at_intersection", r.at_intersection))
                                if arrived_building["type"] in ("bakery", "restaurant"):
                                    arrived_building["revenue"] += random.randint(*REVENUE_PER_VISIT)
                                    arrived_building["customers_served"] += 1
                                    updates.append((destination, "revenue", arrived_building["revenue"]))
                                    updates.append((destination, "customers_served", arrived_building["customers_served"]))
                            updates.append((r.entity, "current_road", None))
                            updates.append((r.entity, "route", None))
                            r.wait_ticks = random.randint(MIN_WAIT_TICKS, MAX_WAIT_TICKS)
                    elif r.wait_ticks > 0:
                        r.wait_ticks -= 1
                    elif r.awaiting_route:
                        pass  # already asked city-routing, just waiting on "route" to arrive
                    elif r.at_home:
                        if random.random() < LEAVE_HOME_CHANCE:
                            if random.random() < VISIT_HOUSE_CHANCE:
                                # A social visit — someone else's house,
                                # never your own. Arriving there already
                                # sets at_home=False via the normal
                                # destination==home_entity check below, so
                                # "linger, then head home" falls out of
                                # the existing state machine for free.
                                candidates = [h for h in buildings_by_type.get("house", []) if h != r.home_entity]
                            else:
                                dest_type = choose_destination_type(city_hour[0])
                                candidates = buildings_by_type.get(dest_type, [])
                            if candidates:
                                destination = random.choice(candidates)
                                r.awaiting_route = True
                                updates.append((r.entity, "desired_destination", destination))
                    else:
                        # Done lingering at a facility — always head home,
                        # keeping the state machine simple and guaranteed
                        # to close out every trip rather than wandering
                        # facility to facility indefinitely.
                        r.awaiting_route = True
                        updates.append((r.entity, "desired_destination", r.home_entity))

                if updates:
                    world.set_many(updates)
                time.sleep(TICK_SECONDS)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
