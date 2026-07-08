"""pgpatchlens CLI: serve / open / install <agent>."""
import argparse
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

PORT = int(os.environ.get("PGPATCHLENS_PORT", "8471"))
BASE = f"http://127.0.0.1:{PORT}"
HOME = Path.home() / ".pgpatchlens"


def _source_root() -> Path | None:
    root = Path(__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").exists() else None


def _cli_cmd() -> str:
    """How agent wrappers should invoke us: source checkout vs installed."""
    root = _source_root()
    return f"uv run --project {root} pgpatchlens" if root else "uvx pgpatchlens"


def _server_up() -> bool:
    try:
        return httpx.get(f"{BASE}/api/entries", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_server() -> bool:
    """Start the server in the background if needed. Returns True if we started it."""
    if _server_up():
        return False
    HOME.mkdir(exist_ok=True)
    log = open(HOME / "server.log", "ab")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "pg_patchlens.server:app", "--port", str(PORT)],
        stdout=log, stderr=log, start_new_session=True,
    )
    for _ in range(60):
        if _server_up():
            return True
        time.sleep(0.25)
    raise SystemExit(f"server failed to start on port {PORT} — see {HOME/'server.log'}")


def cmd_serve(args):
    import uvicorn
    uvicorn.run("pg_patchlens.server:app", port=PORT, host="127.0.0.1")


def cmd_open(args):
    ensure_server()
    url = BASE
    if args.link:
        r = httpx.post(f"{BASE}/api/entries", json={"url": args.link}, timeout=30)
        if r.status_code != 200:
            raise SystemExit(f"could not resolve entry: {r.json().get('detail', r.text)}")
        url = f"{BASE}/#/patch/{r.json()['id']}"
    print(url)
    if not args.no_browser:
        webbrowser.open(url)


SKILL_MD = """---
name: pgpatchlens
description: Review a PostgreSQL commitfest patch in the pg_patchlens web UI. Use when the user runs /pgpatchlens or asks to review a commitfest entry, patch link, or postgr.es thread. Takes the entry URL, bare id, or postgr.es/m link as argument.
---

# pg_patchlens

Open a PostgreSQL commitfest entry in the local pg_patchlens review UI
(three-pane review: grouped diffs, findings, cfbot CI, thread, chat).

Run:

```
{cmd} open "<the user's link or id>"
```

- The command starts the local server if needed, kicks off analysis for new
  entries (takes ~3 minutes; the page streams progress live), opens the
  browser, and prints the URL.
- Give the user the printed URL and tell them analysis streams in live if the
  entry is new.
- No argument? Run `{cmd} open` to open the pg_patchlens landing page.
- The server logs to ~/.pgpatchlens/server.log if something looks wrong.
"""

CODEX_PROMPT_MD = """Open a PostgreSQL commitfest entry in the local pg_patchlens review UI.

Run this shell command with the user's link/id (their message after /pgpatchlens):

    {cmd} open "$ARGUMENTS"

It starts the local server if needed, triggers analysis for new entries
(~3 minutes, progress streams into the page), opens the browser, and prints
the URL. Report the URL back. With no argument, run `{cmd} open`.
"""

OPENCODE_COMMAND_MD = """---
description: Review a PostgreSQL commitfest patch in the pg_patchlens web UI
---

Run this shell command:

    {cmd} open "$ARGUMENTS"

It starts the local pg_patchlens server if needed, triggers analysis for new
entries (~3 minutes, progress streams into the page), opens the browser, and
prints the URL. Report the URL back to the user.
"""

INSTALL_TARGETS = {
    "claude": (Path.home() / ".claude/skills/pgpatchlens/SKILL.md", SKILL_MD),
    "codex": (Path.home() / ".codex/prompts/pgpatchlens.md", CODEX_PROMPT_MD),
    "opencode": (Path.home() / ".config/opencode/command/pgpatchlens.md", OPENCODE_COMMAND_MD),
}


def cmd_install(args):
    targets = INSTALL_TARGETS.keys() if args.agent == "all" else [args.agent]
    for t in targets:
        path, tpl = INSTALL_TARGETS[t]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tpl.format(cmd=_cli_cmd()))
        print(f"{t}: wrote {path}")
    print("\nInvoke with /pgpatchlens <commitfest link or id>")


def main():
    ap = argparse.ArgumentParser(prog="pgpatchlens",
                                 description="pg_patchlens — local PostgreSQL commitfest patch review")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the server in the foreground").set_defaults(fn=cmd_serve)
    p = sub.add_parser("open", help="open an entry (starts the server if needed)")
    p.add_argument("link", nargs="?", help="commitfest URL, bare id, or postgr.es/m link")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_open)
    p = sub.add_parser("install", help="install the /pgpatchlens command for an agent")
    p.add_argument("agent", choices=[*INSTALL_TARGETS, "all"])
    p.set_defaults(fn=cmd_install)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
