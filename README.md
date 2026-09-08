# Sotrice Marketplace

Plugins Sotrice's in-app Marketplace can actually download and install.

`registry.json` at the root lists every published plugin — its type name,
version, description, and which files to fetch. Each plugin's files live
under `plugins/<type>/`.

## Publishing a plugin

1. Add a folder under `plugins/<your-plugin-type>/` with its script and
   any files it imports (e.g. `sotrice_client.py`) — each plugin folder
   is self-contained, so copy in whatever it needs rather than relying on
   another plugin's copy.
2. Add an entry to `registry.json`:

```json
{
  "type": "your-plugin-type",
  "version": "1.0.0",
  "description": "What it does.",
  "concepts": ["WhatItPublishes"],
  "depends_on": [],
  "author": "your-name",
  "entry": "your_script.py",
  "files": ["your_script.py", "sotrice_client.py"]
}
```

3. Bump `version` any time you push new files for an already-published
   `type` — that's what lets Sotrice show "update available" instead of
   silently going stale.
4. Commit and push. Sotrice re-checks this repo's `registry.json` each
   time the Marketplace panel is opened — no separate release/publish
   step needed.

Right now this repo has write access limited to a couple of people while
the install flow is new; opening it up to anyone is the plan once it's
been proven out.
