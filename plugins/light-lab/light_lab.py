"""Light Lab (light-lab) -- the stateful light memory on one leaf.

The simulation runs entirely in the page (light-lab-ui/): a Web Worker path-traces the anchors' light memory,
the page renders from it. This plugin only registers the spin with Core so the Toolbox can open it; it has no
state, no data and no network of its own.
"""

import time

from sotrice_client import World

PLUGIN_TYPE = "light-lab"


def main() -> None:
    with World() as world:
        name = world.identify(PLUGIN_TYPE)
        print(f"[{name}] ready", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print(f"[{name}] stopped", flush=True)


if __name__ == "__main__":
    main()
