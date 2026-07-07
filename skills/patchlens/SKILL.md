---
name: patchlens
description: Review a PostgreSQL commitfest patch in the PatchLens web UI. Use when the user runs /patchlens or asks to review a commitfest entry, patch link, or postgr.es thread. Takes the entry URL, bare id, or postgr.es/m link as argument.
---

# PatchLens

Open a PostgreSQL commitfest entry in the local PatchLens review UI
(three-pane review: grouped diffs, findings, cfbot CI, thread, chat).

Run:

```
uvx --from git+https://github.com/haritabh17/pgpatchlens pgpatchlens open "<the user's link or id>"
```

- The command starts the local server if needed, kicks off analysis for new
  entries (takes ~3 minutes; the page streams progress live), opens the
  browser, and prints the URL.
- Give the user the printed URL and tell them analysis streams in live if the
  entry is new.
- No argument? Run `uvx --from git+https://github.com/haritabh17/pgpatchlens pgpatchlens open` to open the PatchLens landing page.
- Requires `uv` (https://docs.astral.sh/uv/) and a logged-in `claude` or
  `codex` CLI for the analysis LLM. The server logs to
  ~/.pgpatchlens/server.log if something looks wrong.
