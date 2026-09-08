"""City Layout — owns the physical map: a fixed, deliberately simple
topology (one residential street, a 3-lane bypass split, another
residential street) instead of a randomly-generated mesh — legible over
organic, on purpose, so it's actually possible to reason about which of
the 3 lanes traffic prefers on a given run. This is the first of four
cooperating city
plugins (city_layout, city_weather, city_routing, city_residents) —
deliberately four separate plugins, not one big "city plugin," because
the whole point is Routing reading this plugin's data through the Core,
Residents reading Routing's, none of them talking to each other
directly. Realistically a city's roads and buildings get planned
together (not roads first, buildings bolted on later), so this one
plugin owns both, generated once at startup.

Entities published (kind distinguishes them — no Core concept, just an
attribute name every city plugin agrees on out of band, same as
"active"/"beats" are UI-side conventions elsewhere in this project):

  kind="intersection": x, y
  kind="road": from_intersection (entity id), to_intersection (entity
      id), waypoints (list of [x,y], both endpoints included, with a
      couple of interior points nudged sideways for an organic curve
      instead of a straight line), length (total polyline length)
  kind="building": building_type ("house"|"bakery"|"restaurant"|
      "school"|"park"), x, y, nearest_intersection (entity id, the closest
      intersection — Routing treats this as where a resident "enters
      the road network" for last-mile purposes)

Published once with set_many (a hundred-odd entities at several
attributes each — exactly what SetMany exists for), then the process
just idles: this is a one-time-generated static layout, not something
that needs to keep re-announcing itself every tick.
"""

import math
import random
import time

from sotrice_client import World

WORLD_WIDTH = 1600
WORLD_HEIGHT = 1200
# A fixed, deliberately simple topology instead of a randomly-generated
# mesh — "it still isn't clear... just make one street with houses,
# which then splits into three and then merges into one again." This is
# the whole city: one residential street (A-B), a 3-lane bypass section
# (B-D) with no buildings, then another residential street (D-E). Fixed
# on purpose — a controlled layout you can actually reason about
# ("does traffic prefer lane 1, 2, or 3 today"), not a city to explore.
HOUSE_COUNT = 12
BAKERY_COUNT = 1
RESTAURANT_COUNT = 1
SCHOOL_COUNT = 1
PARK_COUNT = 1

# World-space anchor points for the fixed topology (see build_city()).
POINT_A = (100.0, 600.0)
POINT_B = (600.0, 600.0)
POINT_D = (1000.0, 600.0)
POINT_E = (1500.0, 600.0)
LANE_BOWS = (-140.0, 0.0, 140.0)  # perpendicular offset of each of the 3 parallel B-D lanes


def bowed_waypoints(a, b, bow_offset):
    """Like a gentle curve, but with a FIXED, deliberate perpendicular
    bow instead of a random one — used for the 3 parallel lanes so each
    is a stable, visually distinct path (e.g. north / straight / south)
    instead of a randomly-shaped curve that could look similar to its
    siblings from one server run to the next."""
    ax, ay = a
    bx, by = b
    length = math.hypot(bx - ax, by - ay)
    dx, dy = (bx - ax) / length, (by - ay) / length
    perp_x, perp_y = -dy, dx
    mid_x = (ax + bx) / 2 + perp_x * bow_offset
    mid_y = (ay + by) / 2 + perp_y * bow_offset
    # Two extra points between each endpoint and the bow's midpoint keep
    # the curve smooth (a gentle arc) rather than a sharp V through it.
    q1x, q1y = ax + (mid_x - ax) * 0.5, ay + (mid_y - ay) * 0.5
    q3x, q3y = mid_x + (bx - mid_x) * 0.5, mid_y + (by - mid_y) * 0.5
    return [[ax, ay], [q1x, q1y], [mid_x, mid_y], [q3x, q3y], [bx, by]]


def polyline_length(points):
    total = 0.0
    for i in range(len(points) - 1):
        total += math.hypot(points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1])
    return total


def walk_polyline(waypoints, distance):
    """The point at a given ABSOLUTE distance along a polyline (as
    opposed to point_along_road's fraction-of-total), plus which
    segment index that distance falls in — shared by city_routing.py
    (building a route) and city_residents.py (walking one), so both
    interpolate positions identically instead of two slightly-different
    reimplementations drifting apart."""
    covered = 0.0
    for i in range(len(waypoints) - 1):
        ax, ay = waypoints[i]
        bx, by = waypoints[i + 1]
        seg_len = math.hypot(bx - ax, by - ay)
        if distance <= covered + seg_len or i == len(waypoints) - 2:
            local_t = 0 if seg_len == 0 else max(0.0, min(1.0, (distance - covered) / seg_len))
            x = ax + (bx - ax) * local_t
            y = ay + (by - ay) * local_t
            return x, y, i
        covered += seg_len
    x, y = waypoints[-1]
    return x, y, max(0, len(waypoints) - 2)


def point_along_road(waypoints, t):
    """A point a fraction t (0..1) of the way along a polyline by
    arc length, plus the direction of travel there — used to place a
    building "beside" a road at a plausible spot."""
    total = polyline_length(waypoints)
    target = total * t
    covered = 0.0
    for i in range(len(waypoints) - 1):
        ax, ay = waypoints[i]
        bx, by = waypoints[i + 1]
        seg_len = math.hypot(bx - ax, by - ay)
        if covered + seg_len >= target or i == len(waypoints) - 2:
            local_t = 0 if seg_len == 0 else (target - covered) / seg_len
            x = ax + (bx - ax) * local_t
            y = ay + (by - ay) * local_t
            dx, dy = (bx - ax) / (seg_len or 1), (by - ay) / (seg_len or 1)
            return x, y, dx, dy
        covered += seg_len
    x, y = waypoints[-1]
    return x, y, 1.0, 0.0


def build_city():
    """Pure geometry, no Core connection — generates points, roads, and
    a building plan. Kept separate from main() so a standalone script
    can sanity-check the generator itself (connectivity, building
    counts, curve shapes) without needing a running server.

    Fixed topology: A -(0)- B -(1,2,3, parallel)- D -(4)- E. Only roads
    0 and 4 (the two residential streets) get buildings; roads 1-3 are
    pure bypass lanes so city-weather has three visually distinct,
    equally-plausible paths to occasionally flood one of."""
    points = [list(POINT_A), list(POINT_B), list(POINT_D), list(POINT_E)]
    A, B, D, E = 0, 1, 2, 3

    roads = []  # (a_idx, b_idx, waypoints, length) — order matters, see house placement below
    ab_waypoints = bowed_waypoints(points[A], points[B], 0.0)
    roads.append((A, B, ab_waypoints, polyline_length(ab_waypoints)))
    for bow in LANE_BOWS:
        lane_waypoints = bowed_waypoints(points[B], points[D], bow)
        roads.append((B, D, lane_waypoints, polyline_length(lane_waypoints)))
    de_waypoints = bowed_waypoints(points[D], points[E], 0.0)
    roads.append((D, E, de_waypoints, polyline_length(de_waypoints)))
    residential_roads = [roads[0], roads[4]]  # A-B and D-E only — the 3 lanes stay building-free

    building_plan = (
        ["house"] * HOUSE_COUNT
        + ["bakery"] * BAKERY_COUNT
        + ["restaurant"] * RESTAURANT_COUNT
        + ["school"] * SCHOOL_COUNT
        + ["park"] * PARK_COUNT
    )
    random.shuffle(building_plan)

    # Buildings go down as a ROW along a road — evenly spaced, all on the
    # same side, one side chosen per road — instead of each one picking
    # an independent random road/side/offset. That's what actually reads
    # as "a street" instead of scattered dots that happen to be near
    # roads ("you can really put the houses next to another like a
    # normal city, you know that right?" — yes, this is that).
    BUILDING_SPACING = 58
    MARGIN_FROM_JUNCTION = 45

    buildings = []  # (building_type, x, y, nearest_idx)
    shuffled_roads = residential_roads.copy()
    random.shuffle(shuffled_roads)
    plan_index = 0
    for a_idx, b_idx, waypoints, length in shuffled_roads:
        if plan_index >= len(building_plan):
            break
        usable_length = length - 2 * MARGIN_FROM_JUNCTION
        if usable_length <= 0:
            continue
        side = random.choice([-1, 1])       # one side for this whole road
        offset = random.uniform(26, 34)     # one distance-from-road for this whole road
        slot_count = int(usable_length // BUILDING_SPACING) + 1
        for i in range(slot_count):
            if plan_index >= len(building_plan):
                break
            t = min(0.99, (MARGIN_FROM_JUNCTION + i * BUILDING_SPACING) / length)
            x, y, dx, dy = point_along_road(waypoints, t)
            perp_x, perp_y = -dy, dx
            bx, by = x + perp_x * offset * side, y + perp_y * offset * side
            nearest_idx = a_idx if t < 0.5 else b_idx
            buildings.append((building_plan[plan_index], bx, by, nearest_idx))
            plan_index += 1

    # Only reached if the two residential streets' combined row-capacity
    # is smaller than the building plan — fall back to simple random
    # placement (still residential-roads-only) rather than silently
    # dropping buildings.
    while plan_index < len(building_plan):
        a_idx, b_idx, waypoints, _ = random.choice(residential_roads)
        t = random.uniform(0.15, 0.85)
        x, y, dx, dy = point_along_road(waypoints, t)
        perp_x, perp_y = -dy, dx
        side = random.choice([-1, 1])
        bx, by = x + perp_x * 30 * side, y + perp_y * 30 * side
        nearest_idx = a_idx if t < 0.5 else b_idx
        buildings.append((building_plan[plan_index], bx, by, nearest_idx))
        plan_index += 1

    return points, roads, buildings


def main():
    with World() as world:
        name = world.identify("city-layout")
        print(f"[{name}] building the fixed layout: one street, a 3-lane split, one street", flush=True)

        points, roads, buildings = build_city()

        intersection_entities = [world.create_entity() for _ in points]
        updates = []
        for entity, (x, y) in zip(intersection_entities, points):
            updates.append((entity, "kind", "intersection"))
            updates.append((entity, "x", round(x, 1)))
            updates.append((entity, "y", round(y, 1)))

        for a_idx, b_idx, waypoints, length in roads:
            entity = world.create_entity()
            updates.append((entity, "kind", "road"))
            updates.append((entity, "from_intersection", intersection_entities[a_idx]))
            updates.append((entity, "to_intersection", intersection_entities[b_idx]))
            updates.append((entity, "waypoints", [[round(x, 1), round(y, 1)] for x, y in waypoints]))
            updates.append((entity, "length", round(length, 1)))

        print(f"[{name}] {len(roads)} roads connecting them, placing buildings", flush=True)

        for building_type, bx, by, nearest_idx in buildings:
            entity = world.create_entity()
            updates.append((entity, "kind", "building"))
            updates.append((entity, "building_type", building_type))
            updates.append((entity, "x", round(bx, 1)))
            updates.append((entity, "y", round(by, 1)))
            updates.append((entity, "nearest_intersection", intersection_entities[nearest_idx]))
            # Bakeries/restaurants track how business is going — a visible,
            # comparable "is this one doing better than that one" number,
            # kept at 0 by this plugin and incremented by city-residents
            # whenever a resident actually visits (city-layout never
            # touches these again after this initial publish).
            if building_type in ("bakery", "restaurant"):
                updates.append((entity, "revenue", 0))
                updates.append((entity, "customers_served", 0))

        world.set_many(updates)
        print(f"[{name}] published {len(buildings)} buildings, {len(roads)} roads, {len(points)} intersections", flush=True)

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
