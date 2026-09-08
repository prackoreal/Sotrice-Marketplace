"""A deliberately adversarial test fixture: two independent connections
("optimist" and "pessimist") both write the SAME entity's SAME
"confidence" attribute, every tick, in exactly opposite directions.
Not a real Sotrice plugin — the whole point is to give Mindmap View's
contradiction coloring something real to detect and prove itself
against, per the project's own rule that a mechanism isn't verified
until it's been run against an actual conflicting scenario, not just
"it renders."

Both connections share one launch id (SOTRICE_LAUNCH_ID, set by the
server when this is started from the Toolbox) as a common prefix for
their instance names, so unplugging this one Toolbox entry can still
despawn everything both of them drew — see ui/stage3/app.html's
despawnBySource, which matches a push's source against the launch id
either exactly OR as a "<launch_id>-..." prefix, exactly for fixtures
like this one that are more than one Core identity per OS process.
"""

import os
import time

from sotrice_client import World

launch_id = os.environ.get("SOTRICE_LAUNCH_ID", "contradiction-demo-standalone")

with World() as optimist, World() as pessimist:
    optimist_name = optimist.identify("contradiction-demo", instance_id=f"{launch_id}-optimist")
    pessimist_name = pessimist.identify("contradiction-demo", instance_id=f"{launch_id}-pessimist")
    print(f"[{launch_id}] {optimist_name} and {pessimist_name} disagreeing about the same entity", flush=True)

    entity = optimist.create_entity()

    value = 50
    direction = 1
    try:
        while True:
            value += direction * 5
            if value >= 100:
                value = 100
                direction = -1
            elif value <= 0:
                value = 0
                direction = 1
            optimist.set_attribute(entity, "confidence", value)
            pessimist.set_attribute(entity, "confidence", 100 - value)
            time.sleep(0.4)
    except KeyboardInterrupt:
        print(f"[{launch_id}] stopped")
