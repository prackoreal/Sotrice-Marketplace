"""City Routing — reads city_layout's road graph and city_weather's
zones through the Core (never talks to either plugin directly), and
answers "how do I get there" for residents purely through Attributes:
a resident sets "desired_destination" on its own entity, Routing
notices, computes a path, and writes "route" back onto that SAME
entity. No request/response, no RPC — just the same set+subscribe
relay every other plugin uses, which is the entire point of proving
this architecture works for plugins that depend on each other.

Also tracks live congestion: residents report which road they're
currently on ("current_road"), Routing counts occupants per road and
factors that into FUTURE pathfinding — an actual feedback loop where
residents' own movement changes the routes computed for others,
entirely mediated by the Core.

Keeps its own copy of the whole city graph client-side (subscribed
once, replayed on join) — the same "a plugin that needs history keeps
its own" principle the UI's Mindmap view and contradiction tracking
already use, just applied to routing instead of visualization.
"""

import heapq
import math
import time
from collections import defaultdict

from city_layout import walk_polyline
from sotrice_client import World

CONGESTION_WEIGHT = 0.35  # extra cost per resident currently on a road, as a fraction of its length
# Deliberately harsh: with only a handful of roads in the simplified city,
# a mild penalty still loses to "it's just shorter" and residents visibly
# ignore the rain. This needs to dominate almost any detour so avoidance
# is actually visible, not just theoretically present in the math.
WEATHER_WEIGHT = 9.0


class CityGraph:
    def __init__(self):
        self.intersections = {}  # entity -> (x, y)
        self.roads = {}          # entity -> {"from", "to", "waypoints", "length"}
        self.adjacency = defaultdict(list)  # intersection entity -> [(neighbor intersection, road entity)]
        self.buildings = {}      # entity -> {"type", "x", "y", "nearest_intersection"}
        self.weather_zones = {}  # entity -> {"x", "y", "radius", "severity"}
        self.congestion = defaultdict(int)          # road entity -> current occupant count
        self.resident_current_road = {}             # resident entity -> road entity (or None)
        self.resident_at_intersection = {}           # resident entity -> intersection entity

    def road_weather_penalty(self, road):
        """Max severity of any weather zone the road's midpoint passes
        through — recomputed fresh on every route request rather than
        cached, so a storm rolling in immediately affects new routes
        without needing to invalidate anything."""
        waypoints = road["waypoints"]
        mid_x, mid_y = walk_polyline(waypoints, road["length"] / 2)[:2]
        worst = 0.0
        for zone in self.weather_zones.values():
            if math.hypot(mid_x - zone["x"], mid_y - zone["y"]) <= zone["radius"]:
                worst = max(worst, zone["severity"])
        return worst

    def road_weight(self, road_entity):
        road = self.roads[road_entity]
        congestion = self.congestion.get(road_entity, 0)
        weather = self.road_weather_penalty(road)
        return road["length"] * (1 + CONGESTION_WEIGHT * congestion) * (1 + WEATHER_WEIGHT * weather)

    def shortest_path(self, start_intersection, end_intersection):
        """Dijkstra over intersections, edge weight = road_weight().
        Returns the ordered list of road entities used, or None if
        unreachable (shouldn't happen — city_layout guarantees a fully
        connected graph — but a plugin still shouldn't crash if it does)."""
        if start_intersection == end_intersection:
            return []
        distances = {start_intersection: 0.0}
        previous = {}  # intersection -> (previous intersection, road entity used)
        visited = set()
        queue = [(0.0, start_intersection)]
        while queue:
            dist, node = heapq.heappop(queue)
            if node in visited:
                continue
            visited.add(node)
            if node == end_intersection:
                break
            for neighbor, road_entity in self.adjacency[node]:
                weight = self.road_weight(road_entity)
                new_dist = dist + weight
                if new_dist < distances.get(neighbor, float("inf")):
                    distances[neighbor] = new_dist
                    previous[neighbor] = (node, road_entity)
                    heapq.heappush(queue, (new_dist, neighbor))

        if end_intersection not in previous and end_intersection != start_intersection:
            return None

        road_path = []
        node = end_intersection
        while node in previous:
            prev_node, road_entity = previous[node]
            road_path.append(road_entity)
            node = prev_node
        road_path.reverse()
        return road_path

    def build_route(self, road_path, start_intersection, final_point):
        """Concatenates a sequence of roads (as returned by
        shortest_path) into one flat polyline plus a per-segment road-id
        list, oriented consistently from start_intersection through to
        final_point (a building's exact x,y, appended as one last
        straight hop from wherever the last road leaves off)."""
        waypoints = []
        segment_road_ids = []
        current_intersection = start_intersection

        for road_entity in road_path:
            road = self.roads[road_entity]
            pts = road["waypoints"]
            if road["from"] != current_intersection:
                pts = list(reversed(pts))
            # Every road contributes exactly len(pts)-1 segments to the
            # final route regardless of position in the path — the only
            # thing that changes is whether its FIRST point is also
            # appended (skipped after the first road, since it's the
            # same junction point already sitting at the end of
            # `waypoints`, and appending it again would create a
            # zero-length duplicate segment).
            waypoints.extend(pts if not waypoints else pts[1:])
            segment_road_ids.extend([road_entity] * (len(pts) - 1))
            current_intersection = road["to"] if road["from"] == current_intersection else road["from"]

        if not waypoints:
            waypoints = [list(self.intersections[start_intersection])]

        # Last-mile hop from the final intersection to the building's
        # exact position — not part of the road network, just a direct
        # line, matching city_layout's "buildings sit beside a road."
        waypoints.append(list(final_point))
        segment_road_ids.append(None)

        total_length = 0.0
        cumulative = [0.0]
        for i in range(len(waypoints) - 1):
            total_length += math.hypot(
                waypoints[i + 1][0] - waypoints[i][0], waypoints[i + 1][1] - waypoints[i][1]
            )
            cumulative.append(total_length)

        return {
            "waypoints": waypoints,
            "segment_road_ids": segment_road_ids,
            "cumulative_lengths": [round(c, 1) for c in cumulative],
            "length": round(total_length, 1),
        }


def main():
    graph = CityGraph()

    with World() as world:
        name = world.identify("city-routing")
        print(f"[{name}] learning the city from city-layout and city-weather", flush=True)

        # Attributes for a given entity arrive as independent set_attribute
        # calls in no particular order (kind, x, y, ... each their own
        # message) — track every field this plugin cares about per entity
        # in one place, and materialize it into graph.* once a record has
        # everything it needs for its kind.
        fields = defaultdict(dict)

        def remember(entity, attribute, value, source):
            fields[entity][attribute] = value
            record = fields[entity]
            kind = record.get("kind")
            if kind == "intersection" and "x" in record and "y" in record:
                graph.intersections[entity] = (record["x"], record["y"])
            elif kind == "road" and all(k in record for k in ("from_intersection", "to_intersection", "waypoints", "length")):
                if entity not in graph.roads:
                    graph.roads[entity] = {
                        "from": record["from_intersection"],
                        "to": record["to_intersection"],
                        "waypoints": record["waypoints"],
                        "length": record["length"],
                    }
                    graph.adjacency[record["from_intersection"]].append((record["to_intersection"], entity))
                    graph.adjacency[record["to_intersection"]].append((record["from_intersection"], entity))
            elif kind == "building" and all(k in record for k in ("building_type", "x", "y", "nearest_intersection")):
                graph.buildings[entity] = {
                    "type": record["building_type"],
                    "x": record["x"],
                    "y": record["y"],
                    "nearest_intersection": record["nearest_intersection"],
                }
            elif kind == "weather_zone" and all(k in record for k in ("x", "y", "radius", "severity")):
                graph.weather_zones[entity] = {
                    "x": record["x"], "y": record["y"], "radius": record["radius"], "severity": record["severity"],
                }

        for attribute in (
            "kind", "x", "y", "from_intersection", "to_intersection", "waypoints", "length",
            "building_type", "nearest_intersection", "radius", "severity", "condition",
        ):
            world.subscribe(attribute, remember, replay=True)

        def on_current_road(entity, attribute, value, source):
            previous = graph.resident_current_road.get(entity)
            if previous is not None:
                graph.congestion[previous] = max(0, graph.congestion[previous] - 1)
            graph.resident_current_road[entity] = value
            if value is not None:
                graph.congestion[value] += 1

        def on_at_intersection(entity, attribute, value, source):
            graph.resident_at_intersection[entity] = value

        def on_desired_destination(entity, attribute, value, source):
            start = graph.resident_at_intersection.get(entity)
            building = graph.buildings.get(value)
            if start is None or building is None:
                print(f"[{name}] can't route resident {entity} yet (missing data), skipping", flush=True)
                return
            end_intersection = building["nearest_intersection"]
            road_path = graph.shortest_path(start, end_intersection)
            if road_path is None:
                print(f"[{name}] no path found for resident {entity}, skipping", flush=True)
                return
            route = graph.build_route(road_path, start, (building["x"], building["y"]))
            route["destination_building"] = value
            world.set_attribute(entity, "route", route)

        world.subscribe("current_road", on_current_road, replay=True)
        world.subscribe("at_intersection", on_at_intersection, replay=True)
        world.subscribe("desired_destination", on_desired_destination, replay=True)

        print(f"[{name}] ready - {len(graph.intersections)} intersections, {len(graph.roads)} roads known so far", flush=True)

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"[{name}] stopped")


if __name__ == "__main__":
    main()
