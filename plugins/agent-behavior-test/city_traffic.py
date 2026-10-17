"""city_traffic — the population driver that agent_behavior_test.py (and the headless tests) share:
owns the cars and walkers of one area, advances them, runs the invariant guards and returns the
Core updates to publish. No I/O here. Retired agents keep their entity (Core has no deletion):
they are republished with kind "retired" and come back with kind "agent" when the count grows.
"""

from typing import Optional

import city_sim as cs


class CityTraffic:
    def __init__(self, sim: cs.SimWorld, create_entity):
        self.sim = sim
        self.create_entity = create_entity
        self.cars: list[cs.Car] = []
        self.walkers: list[cs.Walker] = []
        self.pool_cars: list[cs.Car] = []
        self.pool_walkers: list[cs.Walker] = []
        self.guards = cs.Guards()
        self._last: dict[int, dict] = {}
        self._pending: list = []
        self.tick_no = 0

    # -- population ------------------------------------------------------------------------------------

    def set_counts(self, walkers: int, vehicles: int) -> None:
        self._resize(self.walkers, self.pool_walkers, cs.Walker, walkers, "walker")
        self._resize(self.cars, self.pool_cars, cs.Car, vehicles, "vehicle")

    def _resize(self, active: list, pool: list, cls, want: int, agent_type: str) -> None:
        while len(active) > want:
            a = active.pop()
            pool.append(a)
            self._pending.append((a.entity, "kind", "retired"))
            self._last.pop(a.entity, None)
        while len(active) < want:
            fresh = not pool
            a = cls(self.create_entity()) if fresh else pool.pop()
            occ = {}
            for c in self.cars:
                occ.setdefault(c.pos_edge, []).append(c)
            if not (a.spawn(self.sim, occ) if cls is cs.Car else a.spawn(self.sim)):
                if fresh:
                    pool.append(a)
                else:
                    pool.append(a)
                break
            if fresh:
                self._pending.append((a.entity, "agent_type", agent_type))
                if agent_type == "vehicle":
                    self._pending.append((a.entity, "vehicle_kind", "car"))
            self._pending.append((a.entity, "kind", "agent"))
            active.append(a)

    # -- one step --------------------------------------------------------------------------------------

    def tick(self, dt: float, settings: dict, publish: bool = True) -> list:
        sim, g = self.sim, self.guards
        self.tick_no += 1
        phys = settings.get("agent_type_physics", {})
        pv, pw = phys.get("vehicle", {}), phys.get("walker", {})
        bias = float(settings.get("building_destination_bias", 0.6))
        gaps = cs.car_gaps(self.cars)
        for c in self.cars:
            gap, lv = gaps.get(c.entity, (None, 0.0))
            c.step(dt, sim, gap, lv, float(pv.get("max_speed_mps", 14.0)), float(pv.get("max_accel_mps2", 1.8)), float(pv.get("max_decel_mps2", 5.0)))
            reason = g.check_car(sim, c)
            if reason:
                g._hit(f"car {c.entity}", reason)
                g.repair(sim, c)
        for w in self.walkers:
            w.step(dt, sim, float(pw.get("max_speed_mps", 1.4)), float(pw.get("max_accel_mps2", cs.WALK_ACCEL)), float(pw.get("max_decel_mps2", cs.WALK_DECEL)), bias)
            reason = g.check_walker(sim, w)
            if reason:
                g._hit(f"walker {w.entity}", reason)
                g.repair(sim, w)
        if not publish:   # an intermediate sub-step of a fast-forward: only the last one publishes
            return []
        out, self._pending = self._pending, []
        for a in self.cars:
            self._publish(out, a, "driving" if a.v > 0.3 else "waiting")
        for a in self.walkers:
            self._publish(out, a, a.state)
        return out

    def _publish(self, out: list, a, state: str) -> None:
        sim = self.sim
        if a.kind == "walker" and a.state in ("inside", "in_station"):
            y_off = -14.0 if a.state == "in_station" else 0.0
            level = "station" if a.state == "in_station" else "inside"
        else:
            y_off = cs.edge_z_offset(sim.net.edges[a.pos_edge]) if a.pos_edge else 0.0
            level = a.level
        last = self._last.setdefault(a.entity, {})
        rx, rz = round(a.x, 2), round(a.z, 2)
        lp = last.get("_xz")
        if lp == (rx, rz) and last.get("_yo") == y_off:
            pos = last["position"]
        else:
            last["_xz"], last["_yo"] = (rx, rz), y_off
            pos = {"x": rx, "y": round(sim.height(a.x, a.z, y_off), 2), "z": rz}
        if last.get("position") != pos:
            last["position"] = pos
            out.append((a.entity, "position", pos))
            if a.state not in ("inside", "in_station"):
                out.append((a.entity, "orientation", cs.yaw_quaternion(a.hx, a.hz)))
        for name, val in (("agent_state", state), ("agent_level", level), ("agent_edge", a.pos_edge)):
            if last.get(name) != val:
                last[name] = val
                out.append((a.entity, name, val))
