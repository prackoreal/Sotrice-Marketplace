"""Physics spin — SKELETON: a synthetic test scene only (a handful of
boxes/spheres falling and colliding), no real map/OSM/3D-Tiles data. That
comes later, from a separate "google 3d tiles spin" feeding this one real
building geometry as ordinary attributes once it exists; this file is
deliberately ignorant of that future producer, the same way it's
deliberately ignorant of any future consumer of what it publishes (an
economics spin reacting to a moved building, say) — it just publishes
per-entity position/velocity/etc., and has no idea who, if anyone, is
watching.

Registered in plugins/registry.json as "physics-test" — start it from the
Toolbox, like any other plugin (see feedback_dogfood_the_ui discipline),
not as a bare background script.

Architecture (translating RAGE/GTA-style open-world engine patterns to
this project, per the design brief this was built from):

- Owns its OWN fixed-timestep loop (`FIXED_DT` below), decoupled from
  whatever render/display rate a future viewer runs at — the accumulator
  pattern in `main()` is the standard idiom for this: real elapsed time
  drains into a bucket, and exactly `FIXED_DT`-sized steps come out of
  it, however many that is on a given real-time pass. There's no
  renderer in this process at all, but the loop is still structured this
  way on purpose, since that's the actual point: a renderer could run at
  any rate without ever changing how fast bodies actually fall.
- Owns an internal uniform spatial grid (`UniformGrid`) for broad-phase
  collision AND general "what's near this point" queries (see
  `UniformGrid.nearby`) — a simple uniform grid is enough at this scale;
  BVH/octree is deliberately not built until profiling says a uniform
  grid isn't (see the project's own "skip it until profiling says
  otherwise" discipline, applied here to spatial partitioning the same
  way it already applies to collision shape fidelity below).
- Uses DOUBLE-PRECISION coordinates from day one: every position/
  velocity component here is a plain Python `float`, which IS IEEE-754
  double precision — there is no separate "use float32" path to
  accidentally fall into, so this requirement is satisfied simply by
  never routing a coordinate through anything that would narrow it (no
  numpy float32 arrays, no `struct.pack("f", ...)`). Real-world geodata
  later needs this; retrofitting precision after the fact is much worse
  than starting with it, per the design brief.
- Publishes per-entity: position, velocity, orientation (a quaternion),
  contact_state, physics_lod_tier. Publishes world-level:
  physics_tick_rate, tick_count.
- Subscribes to an "active_viewpoint"-style attribute to drive LOD
  activation range — nothing publishes one yet, so this starts from a
  hardcoded stub (world origin) per the design brief, but the
  subscription itself is real: if something else ever publishes
  "active_viewpoint" (a future camera/UI plugin), this adopts it exactly
  the way city_weather.py already adopts an externally-set city_hour.

Known, deliberate simplifications (this is a skeleton scene, not a
production physics engine — see the design brief's own "don't
over-engineer ahead of real need" framing):
- Collision (both body-body and body-ground) treats every shape as its
  OWN BOUNDING SPHERE, even a "box" — a box's `half_extents` still
  travels in its published attributes (for a future renderer to draw a
  real box), and it still gets a real, continuously-integrated
  orientation (so it visibly tumbles), but what actually collides is the
  sphere that bounds it. Real OBB-vs-OBB collision is a lot more code for
  a scene whose entire job is "prove objects fall/collide/settle
  correctly," not "be a general physics engine" — upgrading this is a
  clean, isolated follow-up once a real box-shaped consumer needs it.
- No angular response from collisions (a body's angular velocity is
  fixed at spawn and just integrates the orientation quaternion forward
  every tick) — visually convincing tumbling without a full inertia-
  tensor implementation.
"""

import math
import threading
import time

from sotrice_client import World

FIXED_DT = 1.0 / 60.0
PHYSICS_TICK_RATE = 60
GRAVITY = (0.0, -9.81, 0.0)
GROUND_RESTITUTION = 0.45
GROUND_FRICTION = 0.92
BODY_RESTITUTION = 0.35
RESTING_SPEED_EPS = 0.05
# A downward speed at or below this, at the moment of ground contact, is
# treated as "already resting" (clamped to exactly 0) rather than
# reflected — comfortably above the ~0.16 m/s one fixed tick of gravity
# alone induces at PHYSICS_TICK_RATE, so a body sitting still doesn't
# perpetually micro-bounce off gravity's own next-tick nudge (reflecting
# even a tiny incoming speed by GROUND_RESTITUTION every tick never
# actually reaches zero — it approaches a small nonzero fixed point
# instead, since gravity re-adds a fresh tick's worth of speed before the
# next check). A genuine fall impact (several m/s) is well above this
# threshold and still bounces with real restitution.
GROUND_LANDING_SPEED = 0.6

# LOD activation ranges around the current active_viewpoint (world units —
# see the scene layout in `build_scene`, deliberately spread so all three
# tiers are actually exercised by this one small scene rather than every
# body landing in the same tier). Widened from the original 25/50 once
# city-roads/city-buildings' real Klosterneuburg extract existed: the
# real, pathfinding-reachable buildings sit up to ~57 units from the
# scene's own origin (see CHANGELOG's "closing the traffic-counter gap"
# entry) — a real building agent-behavior-test could otherwise never
# actually walk to under "full" simulation.
FULL_SIM_RADIUS = 65.0
KINEMATIC_RADIUS = 110.0

GRID_CELL_SIZE = 8.0

# Steering (see `apply_steering`) — deliberately gentle: this is meant to
# look like "wandering," not "teleporting."
STEER_MAX_ACCEL = 3.0
STEER_MAX_SPEED = 2.5


def vec_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vec_scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def vec_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vec_length(a):
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def vec_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def quat_mul(a, b):
    """Hamilton product of two (x, y, z, w) quaternions."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_normalize(q):
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-12:
        return (0.0, 0.0, 0.0, 1.0)
    return (x / n, y / n, z / n, w / n)


def integrate_orientation(q, angular_velocity, dt):
    """Standard quaternion-derivative integration: dq/dt = 0.5 * (w, 0) * q,
    Euler-stepped then renormalized (renormalizing every step is what
    keeps this numerically stable over thousands of ticks instead of
    slowly drifting off the unit sphere)."""
    wq = (angular_velocity[0], angular_velocity[1], angular_velocity[2], 0.0)
    dq = quat_mul(wq, q)
    stepped = tuple(q[i] + 0.5 * dt * dq[i] for i in range(4))
    return quat_normalize(stepped)


class UniformGrid:
    """A simple broad-phase spatial index: buckets keys into fixed-size
    cells. Rebuilt fresh every tick (`clear()` + `insert(...)` per body) —
    trivial to reason about and plenty fast at this scene's scale; an
    octree/BVH is a real, well-understood upgrade path but not one this
    scene's body count justifies yet."""

    def __init__(self, cell_size: float):
        self.cell_size = cell_size
        self.cells: dict[tuple[int, int, int], list[int]] = {}

    def _cell_of(self, position):
        return (
            math.floor(position[0] / self.cell_size),
            math.floor(position[1] / self.cell_size),
            math.floor(position[2] / self.cell_size),
        )

    def clear(self):
        self.cells.clear()

    def insert(self, key, position):
        self.cells.setdefault(self._cell_of(position), []).append(key)

    def candidate_pairs(self):
        """Every pair of keys that could plausibly be touching — anything
        sharing a cell OR one of its 26 neighbors. A real narrow-phase
        check still has to confirm each pair actually overlaps; this only
        narrows "every pair in the scene" (O(n^2)) down to "pairs that are
        spatially close" (the whole point of a broad phase)."""
        seen: set[tuple[int, int]] = set()
        for (cx, cy, cz), keys in self.cells.items():
            neighbor_keys: list[int] = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        neighbor_keys.extend(self.cells.get((cx + dx, cy + dy, cz + dz), []))
            for key in keys:
                for other in neighbor_keys:
                    if other == key:
                        continue
                    pair = (key, other) if key < other else (other, key)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    yield pair

    def nearby(self, point, radius) -> list:
        """General "what's near this point" query — the same grid used
        for collision broad-phase, usable for anything else that needs
        proximity (a future spin asking "what's near this camera",
        say), not just collision pairs. Coarse: returns everything in
        any cell that could overlap a `radius`-sized ball around
        `point`; an exact distance check is the caller's job, the same
        way `candidate_pairs` only narrows, never confirms."""
        min_cell = self._cell_of(vec_sub(point, (radius, radius, radius)))
        max_cell = self._cell_of(vec_add(point, (radius, radius, radius)))
        out: list = []
        for cx in range(min_cell[0], max_cell[0] + 1):
            for cy in range(min_cell[1], max_cell[1] + 1):
                for cz in range(min_cell[2], max_cell[2] + 1):
                    out.extend(self.cells.get((cx, cy, cz), []))
        return out


class Body:
    """One rigid body in the synthetic scene. `radius` is always the
    BOUNDING sphere used for collision (see the module docstring's
    "known simplifications") — for a box, `half_extents` is separate,
    published metadata only, not consulted by collision code at all."""

    def __init__(self, entity, shape, position, velocity, angular_velocity, radius, half_extents=None, mass=1.0):
        self.entity = entity
        self.shape = shape  # "sphere" | "box"
        self.position = list(position)
        self.velocity = list(velocity)
        self.angular_velocity = angular_velocity
        self.orientation = (0.0, 0.0, 0.0, 1.0)
        self.radius = radius
        self.half_extents = half_extents
        self.mass = mass
        self.inv_mass = 0.0 if mass <= 0 else 1.0 / mass
        self.contact_state = "airborne"
        self.tier = None  # this tick's LOD tier, None until first classified
        self.last_published_tier = None  # the tier `main()` last actually sent an update for


def resolve_ground_collision(body: Body):
    bottom = body.position[1] - body.radius
    if bottom >= 0.0:
        return False
    body.position[1] -= bottom  # push back up to resting height
    if body.velocity[1] < 0.0:
        if abs(body.velocity[1]) <= GROUND_LANDING_SPEED:
            # Already resting, or as good as — see GROUND_LANDING_SPEED's
            # own doc comment for why this must be decided BEFORE
            # reflecting, not by thresholding the reflected result.
            body.velocity[1] = 0.0
        else:
            body.velocity[1] = -body.velocity[1] * GROUND_RESTITUTION
    body.velocity[0] *= GROUND_FRICTION
    body.velocity[2] *= GROUND_FRICTION
    return True


def resolve_body_pair(a: Body, b: Body) -> bool:
    delta = vec_sub(b.position, a.position)
    dist = vec_length(delta)
    overlap = (a.radius + b.radius) - dist
    if overlap <= 0.0 or dist < 1e-9:
        return False
    normal = vec_scale(delta, 1.0 / dist)

    inv_mass_sum = a.inv_mass + b.inv_mass
    if inv_mass_sum > 0.0:
        correction = vec_scale(normal, (overlap / inv_mass_sum) * 0.8)
        a.position[0] -= correction[0] * a.inv_mass
        a.position[1] -= correction[1] * a.inv_mass
        a.position[2] -= correction[2] * a.inv_mass
        b.position[0] += correction[0] * b.inv_mass
        b.position[1] += correction[1] * b.inv_mass
        b.position[2] += correction[2] * b.inv_mass

        relative_velocity = vec_sub(b.velocity, a.velocity)
        approach_speed = vec_dot(relative_velocity, normal)
        if approach_speed < 0.0:
            impulse_mag = -(1.0 + BODY_RESTITUTION) * approach_speed / inv_mass_sum
            impulse = vec_scale(normal, impulse_mag)
            a.velocity[0] -= impulse[0] * a.inv_mass
            a.velocity[1] -= impulse[1] * a.inv_mass
            a.velocity[2] -= impulse[2] * a.inv_mass
            b.velocity[0] += impulse[0] * b.inv_mass
            b.velocity[1] += impulse[1] * b.inv_mass
            b.velocity[2] += impulse[2] * b.inv_mass
    return True


def apply_steering(body: Body, target_xz, dt: float):
    """Nudges a body's HORIZONTAL velocity toward `target_xz` (an (x, z)
    tuple, or `None` for "no target, do nothing") — the write half of a
    watch/write relay: whatever published `desired_target` (a future
    agent-behavior spin, say) doesn't move the body directly, it just
    states an intent; this is the only place that intent turns into an
    actual velocity change, exactly the same "the Core never interprets
    meaning, plugins agree on it out of band" relationship every other
    attribute in this project already has. This file has no idea who (if
    anyone) is publishing `desired_target` — see `main`'s own subscribe
    call — the same ignorance it has of who (if anyone) is publishing
    `active_viewpoint`.

    Capped acceleration and top speed so a distant target can't fling a
    body across the scene in one tick; gravity/ground/collision above
    this in the tick loop are untouched by it either way."""
    if target_xz is None:
        return
    dx = target_xz[0] - body.position[0]
    dz = target_xz[1] - body.position[2]
    distance = math.hypot(dx, dz)
    if distance < 1e-6:
        return
    direction_x, direction_z = dx / distance, dz / distance
    accel = min(STEER_MAX_ACCEL, distance * 2.0)  # eases in as it nears the target
    body.velocity[0] += direction_x * accel * dt
    body.velocity[2] += direction_z * accel * dt
    horizontal_speed = math.hypot(body.velocity[0], body.velocity[2])
    if horizontal_speed > STEER_MAX_SPEED:
        scale = STEER_MAX_SPEED / horizontal_speed
        body.velocity[0] *= scale
        body.velocity[2] *= scale


def lod_tier_for(body: Body, viewpoint) -> str:
    distance = vec_length(vec_sub(body.position, viewpoint))
    if distance <= FULL_SIM_RADIUS:
        return "full"
    if distance <= KINEMATIC_RADIUS:
        return "kinematic"
    return "sleeping"


def step_body(body: Body, tier: str, dt: float):
    """Advances one body by one fixed tick, according to its current LOD
    tier — "full" gets real gravity + ground contact (body-body collision
    is handled separately, across all "full" bodies at once, via the
    grid); "kinematic" still falls and bounces off the ground (cheap,
    genuinely useful — an economics spin watching a moved building still
    wants to see it land) but never collides with another body;
    "sleeping" does nothing at all — this function isn't even called for
    those, see `main`'s tick loop."""
    body.velocity[0] += GRAVITY[0] * dt
    body.velocity[1] += GRAVITY[1] * dt
    body.velocity[2] += GRAVITY[2] * dt
    body.position[0] += body.velocity[0] * dt
    body.position[1] += body.velocity[1] * dt
    body.position[2] += body.velocity[2] * dt
    body.orientation = integrate_orientation(body.orientation, body.angular_velocity, dt)

    on_ground = resolve_ground_collision(body)
    if on_ground:
        horizontal_speed = math.hypot(body.velocity[0], body.velocity[2])
        if abs(body.velocity[1]) < 1e-6 and horizontal_speed < RESTING_SPEED_EPS:
            body.contact_state = "resting"
        else:
            body.contact_state = "colliding"
    elif tier == "full":
        body.contact_state = "airborne"
    else:
        body.contact_state = "airborne"


def build_scene(world: World) -> list[Body]:
    """A handful of boxes/spheres, deliberately spread across three
    distance bands from the world origin (the LOD stub's default
    viewpoint — see the module docstring) so "full"/"kinematic"/
    "sleeping" are all genuinely exercised by this one small scene rather
    than every body landing in the same tier."""
    specs = [
        # (shape, start position, angular velocity, radius/half_extents)
        ("sphere", (-2.0, 9.0, -1.5), (0.4, 0.9, 0.1), 0.6, None),
        ("sphere", (1.5, 11.0, 0.5), (-0.2, 0.3, 0.6), 0.5, None),
        ("box", (0.0, 7.0, 2.0), (0.6, 0.2, -0.4), 0.87, (0.5, 0.5, 0.5)),
        ("box", (-1.5, 5.0, -2.0), (-0.3, 0.5, 0.2), 0.87, (0.5, 0.5, 0.5)),
        ("sphere", (2.5, 6.0, -3.0), (0.1, -0.4, 0.3), 0.4, None),
        ("box", (0.5, 13.0, 0.0), (0.2, -0.2, 0.5), 0.87, (0.5, 0.5, 0.5)),
        # "kinematic"-band cluster (~85 units out, beyond the widened
        # FULL_SIM_RADIUS but inside KINEMATIC_RADIUS): still
        # falls/bounces, never collides with another body.
        ("sphere", (85.0, 8.0, 0.0), (0.0, 0.5, 0.0), 0.6, None),
        ("box", (86.5, 6.0, 1.0), (0.3, 0.1, 0.2), 0.87, (0.5, 0.5, 0.5)),
        ("sphere", (84.0, 10.0, -1.0), (-0.4, 0.2, 0.1), 0.5, None),
        # "sleeping"-band bodies (~170 units out, beyond KINEMATIC_RADIUS):
        # no simulation runs on these at all, once LOD classification notices.
        ("sphere", (170.0, 5.0, 0.0), (0.0, 0.0, 0.0), 0.6, None),
        ("box", (172.0, 5.0, 1.5), (0.1, 0.1, 0.1), 0.87, (0.5, 0.5, 0.5)),
    ]

    bodies = []
    for shape, position, angular_velocity, radius, half_extents in specs:
        entity = world.create_entity()
        body = Body(
            entity=entity,
            shape=shape,
            position=position,
            velocity=(0.0, 0.0, 0.0),
            angular_velocity=angular_velocity,
            radius=radius,
            half_extents=half_extents,
            mass=1.0,
        )
        world.set_attribute(entity, "shape", shape)
        if half_extents is not None:
            world.set_attribute(entity, "half_extents", {"x": half_extents[0], "y": half_extents[1], "z": half_extents[2]})
        else:
            world.set_attribute(entity, "radius", radius)
        bodies.append(body)
    return bodies


def main():
    with World() as world:
        name = world.identify("physics-test")
        print(f"[{name}] building the synthetic test scene")

        world_entity = world.create_entity()
        world.set_attribute(world_entity, "physics_tick_rate", PHYSICS_TICK_RATE)
        world.set_attribute(world_entity, "tick_count", 0)

        # Hardcoded stub, per the design brief — nothing publishes a real
        # "active_viewpoint" yet. The subscription below is still real:
        # if a future plugin ever does publish one, this adopts it live,
        # the same pattern city_weather.py already uses for city_hour.
        viewpoint_lock = threading.Lock()
        viewpoint = [0.0, 0.0, 0.0]

        def on_viewpoint(entity, attribute, value, source):
            with viewpoint_lock:
                viewpoint[0] = float(value.get("x", 0.0))
                viewpoint[1] = float(value.get("y", 0.0))
                viewpoint[2] = float(value.get("z", 0.0))

        world.subscribe("active_viewpoint", on_viewpoint, replay=True)

        # Generic "someone wants this entity to head somewhere" channel —
        # see `apply_steering`'s own doc comment. This file has no idea
        # who publishes it (an agent-behavior spin's wander logic, say,
        # or a human dragging a target in a future UI) — it just watches
        # the attribute and turns it into a velocity nudge, the same
        # ignorance-of-the-consumer/producer this scene already has for
        # `active_viewpoint`.
        desired_targets_lock = threading.Lock()
        desired_targets: dict[int, tuple[float, float]] = {}

        def on_desired_target(entity, attribute, value, source):
            with desired_targets_lock:
                desired_targets[entity] = (float(value.get("x", 0.0)), float(value.get("z", 0.0)))

        world.subscribe("desired_target", on_desired_target, replay=True)

        bodies = build_scene(world)
        grid = UniformGrid(GRID_CELL_SIZE)

        tick_count = 0
        accumulator = 0.0
        last_time = time.perf_counter()

        print(f"[{name}] running {len(bodies)} bodies at {PHYSICS_TICK_RATE}Hz — Ctrl+C to stop")
        try:
            while True:
                now = time.perf_counter()
                accumulator += now - last_time
                last_time = now

                # The fixed-timestep accumulator: however much real time
                # actually passed, physics advances in exact FIXED_DT
                # steps — decoupled from this loop's own real-time
                # jitter, and from any future render rate entirely.
                while accumulator >= FIXED_DT:
                    with viewpoint_lock:
                        current_viewpoint = tuple(viewpoint)

                    for body in bodies:
                        body.tier = lod_tier_for(body, current_viewpoint)

                    grid.clear()
                    full_tier_indices = []
                    for i, body in enumerate(bodies):
                        if body.tier == "sleeping":
                            continue
                        if body.tier == "kinematic":
                            step_body(body, body.tier, FIXED_DT)
                            continue
                        full_tier_indices.append(i)
                        grid.insert(i, body.position)

                    # Every key ever inserted into `grid` this tick is a
                    # "full" tier index (see the loop above) — kinematic
                    # and sleeping bodies were never inserted at all, so
                    # `candidate_pairs()` can only ever yield full-tier
                    # pairs here, with no extra filtering needed.
                    for i, j in grid.candidate_pairs():
                        if resolve_body_pair(bodies[i], bodies[j]):
                            bodies[i].contact_state = "colliding"
                            bodies[j].contact_state = "colliding"

                    with desired_targets_lock:
                        current_targets = dict(desired_targets)
                    for i in full_tier_indices:
                        apply_steering(bodies[i], current_targets.get(bodies[i].entity), FIXED_DT)
                        step_body(bodies[i], "full", FIXED_DT)

                    tick_count += 1
                    accumulator -= FIXED_DT

                updates = [(world_entity, "tick_count", tick_count)]
                for body in bodies:
                    if body.tier == "sleeping":
                        # Publish once on the transition into sleeping so
                        # a subscriber can see it stopped, then stay
                        # silent — "not at all" for a genuinely far body,
                        # per the design brief.
                        if body.tier != body.last_published_tier:
                            updates.append((body.entity, "physics_lod_tier", body.tier))
                        body.last_published_tier = body.tier
                        continue
                    updates.append((body.entity, "position", {"x": body.position[0], "y": body.position[1], "z": body.position[2]}))
                    updates.append((body.entity, "velocity", {"x": body.velocity[0], "y": body.velocity[1], "z": body.velocity[2]}))
                    updates.append((body.entity, "orientation", {"x": body.orientation[0], "y": body.orientation[1], "z": body.orientation[2], "w": body.orientation[3]}))
                    updates.append((body.entity, "contact_state", body.contact_state))
                    updates.append((body.entity, "physics_lod_tier", body.tier))
                    body.last_published_tier = body.tier

                world.set_many(updates)

                # Sleep off whatever's left before the next FIXED_DT would
                # be due, rather than a fixed sleep(FIXED_DT) — keeps this
                # loop's own overhead from silently adding drift on top of
                # the accumulator's real-time tracking.
                remaining = FIXED_DT - accumulator
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            print(f"[{name}] stopped after {tick_count} ticks")


if __name__ == "__main__":
    main()
