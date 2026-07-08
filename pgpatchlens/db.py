# ponytail: sqlite via stdlib; swap DSN for Postgres when this leaves one laptop
import json
import os
import sqlite3
import threading
from pathlib import Path


def _db_path() -> Path:
    if p := os.environ.get("PGPATCHLENS_DB"):
        return Path(p)
    root = Path(__file__).resolve().parent.parent
    if (root / "pyproject.toml").exists():      # running from a source checkout
        return root / "patchlens.db"
    d = Path.home() / ".pgpatchlens"            # installed (pipx/uvx): user data dir
    d.mkdir(exist_ok=True)
    return d / "patchlens.db"


DB_PATH = _db_path()
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY,           -- commitfest patch id
  cf_id TEXT, title TEXT, status TEXT, target_version TEXT,
  authors TEXT, reviewers TEXT, thread_msgid TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS series(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER, version TEXT, msgid TEXT,
  applies INTEGER, base_sha TEXT, head_sha TEXT, fetched_at TEXT,
  diff TEXT,
  UNIQUE(entry_id, version)
);
CREATE TABLE IF NOT EXISTS patches(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id INTEGER, ordinal INTEGER, filename TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id INTEGER, ordinal INTEGER, title TEXT,
  explanation_md TEXT, snippet TEXT, files TEXT       -- files: json list
);
CREATE TABLE IF NOT EXISTS findings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id INTEGER, severity TEXT, title TEXT, detail TEXT,
  file TEXT, line_from INTEGER, line_to INTEGER,
  state TEXT DEFAULT 'open', fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS ci_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  series_id INTEGER, task TEXT, status TEXT, detail_url TEXT
);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER, msgid TEXT, author TEXT, sent_at TEXT, body_excerpt TEXT,
  UNIQUE(entry_id, msgid)
);
CREATE TABLE IF NOT EXISTS analysis(
  entry_id INTEGER PRIMARY KEY,
  overview_md TEXT, summary_md TEXT, thread_summary_md TEXT, state TEXT
);
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER, file TEXT, line INTEGER, side TEXT, body TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER, user_token TEXT, role TEXT, content TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS finding_reads(
  finding_id INTEGER, user_token TEXT,
  UNIQUE(finding_id, user_token)
);
"""

MIGRATIONS = (
    "ALTER TABLE comments ADD COLUMN line_to INTEGER",
    "ALTER TABLE comments ADD COLUMN user_token TEXT DEFAULT 'anon'",
)


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        for stmt in MIGRATIONS:
            try:
                c.execute(stmt)
                c.commit()
            except sqlite3.OperationalError:
                pass
        _local.conn = c
    return c


def rows(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def run(sql, args=()):
    cur = conn().execute(sql, args)
    conn().commit()
    return cur.lastrowid


def entry_state(entry_id: int, user: str = "anon") -> dict:
    """Everything the UI needs for one entry. Shared analysis + this user's
    comments, chat, and finding read-state."""
    e = one("SELECT * FROM entries WHERE id=?", (entry_id,))
    if not e:
        return {}
    s = one("SELECT id,entry_id,version,msgid,applies,base_sha,head_sha,fetched_at FROM series WHERE entry_id=? ORDER BY id DESC", (entry_id,))
    sid = s["id"] if s else -1
    diff_row = one("SELECT diff FROM series WHERE id=?", (sid,)) or {}
    a = one("SELECT * FROM analysis WHERE entry_id=?", (entry_id,)) or {}
    changes = rows("SELECT * FROM changes WHERE series_id=? ORDER BY ordinal", (sid,))
    for c in changes:
        c["files"] = json.loads(c["files"] or "[]")
    return {
        "entry": e,
        "series": s,
        "diff": diff_row.get("diff"),
        "patches": rows("SELECT * FROM patches WHERE series_id=? ORDER BY ordinal", (sid,)),
        "changes": changes,
        "findings": rows(
            "SELECT f.*, (fr.finding_id IS NOT NULL) AS read FROM findings f "
            "LEFT JOIN finding_reads fr ON fr.finding_id=f.id AND fr.user_token=? "
            "WHERE f.series_id=?", (user, sid)),
        "ci_runs": rows("SELECT * FROM ci_runs WHERE series_id=?", (sid,)),
        "messages": rows("SELECT * FROM messages WHERE entry_id=? ORDER BY sent_at", (entry_id,)),
        "analysis": a,
        "comments": rows("SELECT * FROM comments WHERE entry_id=? AND user_token=? ORDER BY id", (entry_id, user)),
        "chat": rows("SELECT role, content FROM chat_messages WHERE entry_id=? AND user_token=? ORDER BY id", (entry_id, user)),
    }


if __name__ == "__main__":
    run("INSERT OR REPLACE INTO entries(id,title) VALUES(999999,'self test')")
    assert one("SELECT * FROM entries WHERE id=999999")["title"] == "self test"
    run("DELETE FROM entries WHERE id=999999")
    print("db ok")
