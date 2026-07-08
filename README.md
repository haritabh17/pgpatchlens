# PatchLens

Makes reviewing PostgreSQL commitfest patches easier. Paste a commitfest
entry (URL, bare id, or `postgr.es/m/` thread link) and get:
grouped, explained diffs · Bug/Investigate/Informational findings anchored to
file:line · cfbot CI status · thread summary · inline draft comments composed
into a ready-to-send pgsql-hackers reply.

## Install (from your coding agent)

After installing, type `/patchlens <link>` — the agent launches/reuses the
local server and hands you the review URL. Requires
[`uv`](https://docs.astral.sh/uv/) plus a logged-in `claude` or `codex` CLI
for the analysis LLM (your subscription pays; no API key needed).

### Claude Code

```
/plugin marketplace add haritabh17/pgpatchlens
```
```
/plugin install patchlens@pgpatchlens
```

### Codex

```bash
codex plugin marketplace add haritabh17/pgpatchlens
codex plugin add patchlens@pgpatchlens
```

### Pi agent harness

```bash
pi install git:github.com/haritabh17/pgpatchlens
```

### OpenCode (or any agent, or no agent)

```bash
uvx --from git+https://github.com/haritabh17/pgpatchlens pgpatchlens install opencode
# or skip agents entirely:
uvx --from git+https://github.com/haritabh17/pgpatchlens pgpatchlens open <link>
```

LLM backend (auto-detected, override with `PGPATCHLENS_LLM=api|claude|codex`):
the Anthropic API when `ANTHROPIC_API_KEY` is set, else the logged-in `claude`
CLI, else the logged-in `codex` CLI — so analysis and chat run on the user's
own subscription with zero key management. The landing page shows the active
backend, model, and account.

### Configuration (env vars, read at server start)

| Variable | Effect |
|---|---|
| `PGPATCHLENS_MODEL` | Model on any backend (default `claude-opus-4-8`; Codex uses its own default when unset). |
| `CLAUDE_CONFIG_DIR` | Which Claude login to use — point at a per-account config dir to pick a subscription (e.g. `~/.claude-work`). Shown as the account on the status line. |
| `PGPATCHLENS_PORT` / `PGPATCHLENS_DB` | Server port / SQLite path. |

These are read once when the server starts, so change them by relaunching:

```sh
CLAUDE_CONFIG_DIR="$HOME/.claude-work" PGPATCHLENS_MODEL=claude-sonnet-5 uv run pgpatchlens serve
```

## How it works

```
entry URL ─► scrape commitfest entry page (title/status/authors/thread msgid)
          ─► scrape postgresql.org flat thread (messages + latest patch series)
          ─► GitHub compare master...cf/<id> on postgresql-cfbot mirror
             (applied diff + merge-base, no git clone)  +  check-runs (CI)
          ─► LLM pass A: group hunks into reviewable changes + overview
          ─► LLM pass B: per-group explanations + key snippets
          ─► LLM pass C: findings (PG review heuristics; anchors validated
             against the diff — hallucinated file:line gets dropped)
          ─► LLM pass D: thread summary
          each step streams to the UI over SSE; review is useful ~10s in
```

The UI follows the light PatchLens design comp (Postgres-blue band, Source
Serif/Sans/Code Pro). Diffs are expanded by default with an inline /
side-by-side toggle (⚙ View, also `?view=sbs`); the red "viewed" checkbox on a
file collapses it; the left/right rails are drag-resizable (persisted); the
sticky review bar previews the composed draft reply whenever comments exist;
the landing page lists previously analyzed entries, each removable, and a
review can be re-run (`⟳ re-analyze`) when a new patch version lands —
draft comments and chat survive the re-run.

Multi-user without login: the browser mints an anonymous UUID (localStorage)
and sends it as `X-PatchLens-User` on every call. Analysis/findings/CI/thread
are shared per entry; draft comments, chat history, and finding read-state are
private per token. A future community-account login claims the token's rows.

Storage is SQLite (`patchlens.db`), schema mirrors the eventual Postgres one.
Draft comments anchor to `(file, new-line)`; "Finish review" composes one
email — summary on top, each comment as quoted diff context — with
`In-Reply-To`/`References` set to the latest thread message, plus a `mailto:`
handoff (zero-infrastructure sending: the mail comes from *you*).

## Development (from a checkout)

```sh
uv sync
uv run pgpatchlens open <link>         # or: uv run pgpatchlens serve
uv run python pgpatchlens/db.py        # self-test: schema round-trip
uv run python pgpatchlens/analyze.py   # self-test: diff parser + finding validator
uv run python pgpatchlens/ingest.py    # self-test: live, scrapes a real entry
```

Background-server logs land in `~/.pgpatchlens/server.log`.
