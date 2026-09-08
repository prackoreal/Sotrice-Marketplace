# Sotrice Marketplace

Plugins Sotrice's in-app Marketplace can actually download and install.

Each plugin is a **`.spin`** file (Sotrice plugin) — a plain zip archive
containing the plugin's script(s) plus a `manifest.json` describing it.
`registry.json` at the root lists every published plugin for browsing
(type, version, description) and which `.spin` file to fetch; the
`.spin` you actually install from is the authoritative source for
everything else.

## Publishing a plugin

1. Add a folder under `plugins/<your-plugin-type>/` with your script,
   any files it imports (e.g. `sotrice_client.py` — each plugin folder
   is self-contained, copy in whatever it needs rather than relying on
   another plugin's copy), and a `manifest.json`:

```json
{
  "type": "your-plugin-type",
  "version": "1.0.0",
  "description": "What it does.",
  "concepts": ["WhatItPublishes"],
  "depends_on": [],
  "author": "your-name",
  "entry": "your_script.py",
  "handles_extensions": [],
  "ui_controls": []
}
```

   `handles_extensions` (e.g. `[".spy"]`) and `ui_controls` (`"clock"`
   or `"counter"`) are only needed if your plugin actually declares one
   — see Sotrice's own `clients/python/` for real examples.

2. Zip the folder's *contents* (not the folder itself) into
   `plugins/<your-plugin-type>.spin`:

   ```
   Compress-Archive -Path plugins/your-plugin-type/* -DestinationPath plugins/your-plugin-type.zip
   ```
   then rename the `.zip` to `.spin` (`Compress-Archive` insists on the
   `.zip` extension; the file format itself is identical).

3. Add or update its entry in `registry.json` — a browsing-only copy of
   the same metadata, plus which file to fetch:

```json
{
  "type": "your-plugin-type",
  "version": "1.0.0",
  "description": "What it does.",
  "concepts": ["WhatItPublishes"],
  "depends_on": [],
  "author": "your-name",
  "file": "your-plugin-type.spin"
}
```

4. Bump `version` in BOTH `manifest.json` and `registry.json` any time
   you re-publish an already-published `type` — that's what lets
   Sotrice show "update available" instead of silently going stale.
5. Commit and push. Sotrice re-checks this repo's `registry.json` each
   time the Marketplace panel is opened — no separate release/publish
   step needed.

Right now this repo has write access limited to a couple of people while
the install flow is new; opening it up to anyone is the plan once it's
been proven out.
