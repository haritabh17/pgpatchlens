# PatchLens

Devin-Review-style analysis for PostgreSQL commitfest patches. Paste a
commitfest entry (URL, bare id, or `postgr.es/m/` thread link) and get:
grouped, explained diffs · Bug/Investigate/Informational findings anchored to
file:line · cfbot CI status · thread summary · inline draft comments composed
into a ready-to-send pgsql-hackers reply.

## Run

```sh
uv sync
uv run pgpatchlens open <commitfest link or id>   # starts server, opens browser
# or: uv run pgpatchlens serve                     # foreground server on :8471
```

Background-server logs land in `~/.pgpatchlens/server.log`.

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
own subscription with zero key management.

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
the landing page lists previously analyzed entries.

Multi-user without login: the browser mints an anonymous UUID (localStorage)
and sends it as `X-PatchLens-User` on every call. Analysis/findings/CI/thread
are shared per entry; draft comments, chat history, and finding read-state are
private per token. A future community-account login claims the token's rows.

Storage is SQLite (`patchlens.db`), schema mirrors the eventual Postgres one.
Draft comments anchor to `(file, new-line)`; "Finish review" composes one
email — summary on top, each comment as quoted diff context — with
`In-Reply-To`/`References` set to the latest thread message, plus a `mailto:`
handoff (zero-infrastructure sending: the mail comes from *you*).

## Self-tests

```sh
uv run python pgpatchlens/db.py        # schema round-trip
uv run python pgpatchlens/analyze.py   # diff parser + finding validator
uv run python pgpatchlens/ingest.py    # live: scrapes a real entry end to end
```

## Deferred (add when needed)

- Community-account SSO + posting directly to the commitfest app (compose/mailto covers the loop)
- Finding diffing across patch versions ("resolved in v14") — `fingerprint` column already exists
- Scheduled polling, tree-sitter symbol index (hunk headers carry function names already)
- Re-analysis of a completed entry (currently cached forever; add `?force=1`)
- Direct SMTP send from the UI (mailto/copy covers it); public shared-analysis cache for local instances
