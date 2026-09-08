"""A genuinely Marketplace-only plugin — never bundled with Sotrice
itself, exists to prove a plugin can be published, downloaded, run, and
removed entirely through the Marketplace panel, with nothing pre-shipped
for it. Flips a coin once a second and publishes the running tally.
"""

import random
import time

from sotrice_client import World

with World() as world:
    name = world.identify("coin-flip")
    print(f"[{name}] flipping a coin once a second", flush=True)

    entity = world.create_entity()
    heads = 0
    tails = 0

    while True:
        if random.random() < 0.5:
            heads += 1
        else:
            tails += 1
        world.set_many([
            (entity, "heads_count", heads),
            (entity, "tails_count", tails),
        ])
        print(f"[{name}] heads={heads} tails={tails}", flush=True)
        time.sleep(1)
