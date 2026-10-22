# Dev channel

Two branches of this repo are served to the app:

| Channel | Branch | Who gets it |
|---|---|---|
| Stable | `main` | every user (this is what the in-app Marketplace reads by default) |
| Dev | `dev` | only an app whose Settings > Developer > "Spin channel" is set to Dev |

Nothing we build reaches other users untested.

## Flow

1. **A lane builds a spin** in its own worktree and commits on its own branch. It never pushes.
2. **The Manager session puts the build on `dev`**: builds the `.spin` with `scripts/build_spin.py <type> --marketplace <this checkout on branch dev>`, bumps the version in `registry.json` **above main's**, adds `min_app_version` if the spin needs a newer app, commits on `dev`, and pushes `dev`.
3. **Laszlo tests it in his real app**: Settings > Developer > Spin channel > Dev, then Marketplace > Download/Update. Dev-installed spins carry a **Dev** badge in the Toolbox and the Marketplace.
4. **After his OK**, the Manager merges `dev` into `main` and pushes `main`. Only then do other users receive it. If he does not OK it, the lane fixes it and step 2 repeats; `main` never changes.
5. **Back to Stable**: Settings > Developer > Spin channel > Stable reinstalls the stable version of every spin that came from Dev (a normal update check would not: Dev versions are higher) and removes Dev-only spins.

Keep `dev` a superset of `main`: after every promotion, `git merge main` into `dev` (or fast-forward it) so `dev` never lacks a stable fix.

## Unlocking the Developer tab

The tab is hidden on purpose. In Settings > Updates, click the "Sotrice <version>" line 7 times (each click within 3 seconds of the last).

## min_app_version

A `registry.json` plugin entry may carry `"min_app_version": "0.1.33"`. An app older than that does not offer the entry (the card says "Needs Sotrice 0.1.33+"), refuses to install it, and the auto-update sweep skips it. Set it on every spin that depends on a server/shell change shipped in that app version, on `dev` and on `main` alike. Versions compare numerically per dot segment.

## Real data stays safe

Dev and Stable share one data folder, deliberately: Laszlo must be able to test a new math-teacher against his real classes. The protection is:

- **Automatic backup before the first Dev install.** Switching to Dev makes the server copy the classes folder and the student identity store to `<Sotrice data>/backups/dev-switch-<unix time>/` (newest 3 kept). If the backup fails, the switch is aborted.
- **Additive-only data formats.** A spin must keep reading everything an older version wrote, and must only add fields. A change that older spins cannot read needs a migration plus a `min_app_version`, and must be called out in the lane's report so it is not promoted without a look.
- **Restore** (if a Dev build damaged data): quit Sotrice, copy `classes/` and `students.json` out of the newest `backups/dev-switch-*` folder over the live ones, restart.

## Checks before promoting dev -> main

- Every changed spin's archive contains the new code (build_spin verifies this itself).
- Versions on `dev` are above `main`'s; `min_app_version` is set where needed and the matching app is already released.
- Laszlo tested it on Dev.
