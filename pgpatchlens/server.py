"""PatchLens API: REST + SSE + static UI."""
import asyncio
import json
import threading
import traceback
import urllib.parse
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# anonymous per-browser identity; a future community-account login claims these tokens
UserToken = Header("anon", alias="X-PatchLens-User")

from pgpatchlens import analyze, db, ingest

app = FastAPI(title="PatchLens")
STATIC = Path(__file__).resolve().parent / "static"

# per-entry progress: list of step names; appended by the worker thread
_progress: dict[int, list[str]] = {}
_running: set[int] = set()
_lock = threading.Lock()


def _emit(entry_id: int, step: str):
    _progress.setdefault(entry_id, []).append(step)


def run_pipeline(entry_id: int):
    try:
        e = ingest.fetch_entry(entry_id)
        db.run("INSERT OR REPLACE INTO entries(id,cf_id,title,status,target_version,authors,reviewers,thread_msgid) "
               "VALUES(?,?,?,?,?,?,?,?)",
               (e["id"], e["cf_id"], e["title"], e["status"], e["target_version"],
                e["authors"], e["reviewers"], e["thread_msgid"]))
        db.run("INSERT OR REPLACE INTO analysis(entry_id,state) VALUES(?, 'running')", (entry_id,))
        _emit(entry_id, "entry")

        thread = {"messages": [], "patches": [], "version": e["latest_version"]}
        if e["thread_msgid"]:
            thread = ingest.fetch_thread(e["thread_msgid"])
            for m in thread["messages"]:
                db.run("INSERT OR IGNORE INTO messages(entry_id,msgid,author,sent_at,body_excerpt) VALUES(?,?,?,?,?)",
                       (entry_id, m["msgid"], m["author"], m["sent_at"], m["body"]))
        _emit(entry_id, "thread")

        d = ingest.fetch_diff(entry_id)
        db.run("DELETE FROM series WHERE entry_id=?", (entry_id,))
        sid = db.run("INSERT INTO series(entry_id,version,msgid,applies,base_sha,head_sha,fetched_at,diff) "
                     "VALUES(?,?,?,?,?,?,datetime('now'),?)",
                     (entry_id, thread["version"], e["thread_msgid"], int(d["applies"]),
                      d.get("base_sha"), d.get("head_sha"), d.get("diff")))
        for i, p in enumerate(thread["patches"]):
            db.run("INSERT INTO patches(series_id,ordinal,filename,url) VALUES(?,?,?,?)",
                   (sid, i, p["filename"], p["url"]))
        _emit(entry_id, "diff")

        for ci in ingest.fetch_ci(entry_id):
            db.run("INSERT INTO ci_runs(series_id,task,status,detail_url) VALUES(?,?,?,?)",
                   (sid, ci["task"], ci["status"], ci["detail_url"]))
        _emit(entry_id, "ci")

        if not d["applies"]:
            db.run("UPDATE analysis SET state='no_branch' WHERE entry_id=?", (entry_id,))
            _emit(entry_id, "done")
            return

        files = analyze.parse_diff(d["diff"])
        a = analyze.pass_a_group(e, files)
        db.run("UPDATE analysis SET overview_md=?, summary_md=? WHERE entry_id=?",
               (a["overview_md"], a["summary_md"], entry_id))
        for i, g in enumerate(a["groups"]):
            db.run("INSERT INTO changes(series_id,ordinal,title,files) VALUES(?,?,?,?)",
                   (sid, i + 1, g["title"], json.dumps(g["files"])))
        _emit(entry_id, "groups")

        for ex in analyze.pass_b_explain(e, a["groups"], d["diff"]):
            db.run("UPDATE changes SET explanation_md=?, snippet=? WHERE series_id=? AND ordinal=?",
                   (ex["explanation_md"], ex.get("snippet"), sid, ex["ordinal"]))
        _emit(entry_id, "explanations")

        for fd in analyze.validate_findings(analyze.pass_c_findings(e, d["diff"]), files):
            db.run("INSERT INTO findings(series_id,severity,title,detail,file,line_from,line_to,fingerprint) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (sid, fd["severity"], fd["title"], fd.get("detail", ""), fd["file"],
                    fd.get("line_from"), fd.get("line_to"), fd["fingerprint"]))
        _emit(entry_id, "findings")

        if thread["messages"]:
            ts = analyze.pass_d_thread(e, thread["messages"])
            db.run("UPDATE analysis SET thread_summary_md=? WHERE entry_id=?", (ts, entry_id))
        _emit(entry_id, "thread_summary")

        db.run("UPDATE analysis SET state='done' WHERE entry_id=?", (entry_id,))
        _emit(entry_id, "done")
    except Exception:
        traceback.print_exc()
        db.run("INSERT OR REPLACE INTO analysis(entry_id,state) VALUES(?, 'error')", (entry_id,))
        _emit(entry_id, "error")
    finally:
        with _lock:
            _running.discard(entry_id)


class EntryReq(BaseModel):
    url: str


@app.post("/api/entries")
def create_entry(req: EntryReq):
    entry_id = ingest.normalize(req.url)
    if not entry_id:
        raise HTTPException(400, "could not resolve a commitfest entry from that input")
    # ponytail: done = cached forever; add ?force=1 or staleness checks when needed
    a = db.one("SELECT state FROM analysis WHERE entry_id=?", (entry_id,))
    if a and a["state"] == "done":
        _progress.setdefault(entry_id, []).append("done")
        return {"id": entry_id}
    with _lock:
        if entry_id not in _running:
            _running.add(entry_id)
            # clear previous run's derived rows
            sid = db.one("SELECT id FROM series WHERE entry_id=?", (entry_id,))
            if sid:
                for t in ("patches", "changes", "findings", "ci_runs"):
                    db.run(f"DELETE FROM {t} WHERE series_id=?", (sid["id"],))
            db.run("DELETE FROM messages WHERE entry_id=?", (entry_id,))
            _progress[entry_id] = []
            threading.Thread(target=run_pipeline, args=(entry_id,), daemon=True).start()
    return {"id": entry_id}


@app.get("/api/entries")
def list_entries():
    return db.rows(
        "SELECT e.id, e.title, e.status, a.state, s.version, s.fetched_at "
        "FROM entries e LEFT JOIN analysis a ON a.entry_id=e.id "
        "LEFT JOIN series s ON s.entry_id=e.id "
        "ORDER BY s.fetched_at DESC LIMIT 20")


@app.post("/api/entries/{entry_id}/findings/read")
def mark_read(entry_id: int, user: str = UserToken):
    sid = db.one("SELECT id FROM series WHERE entry_id=? ORDER BY id DESC", (entry_id,))
    if sid:
        db.run("INSERT OR IGNORE INTO finding_reads(finding_id, user_token) "
               "SELECT id, ? FROM findings WHERE series_id=?", (user, sid["id"]))
    return {"ok": True}


@app.get("/api/entries/{entry_id}")
def get_entry(entry_id: int, user: str = UserToken):
    st = db.entry_state(entry_id, user)
    if not st:
        raise HTTPException(404, "unknown entry")
    return st


@app.get("/api/entries/{entry_id}/stream")
async def stream(entry_id: int):
    async def gen():
        seen = 0
        while True:
            evs = _progress.get(entry_id, [])
            while seen < len(evs):
                yield f"data: {json.dumps({'step': evs[seen]})}\n\n"
                if evs[seen] in ("done", "error"):
                    return
                seen += 1
            await asyncio.sleep(0.4)  # ponytail: poll, not condvars — single process
    return StreamingResponse(gen(), media_type="text/event-stream")


class CommentReq(BaseModel):
    file: str
    line: int
    line_to: int | None = None
    side: str = "new"
    body: str


@app.post("/api/entries/{entry_id}/comments")
def add_comment(entry_id: int, req: CommentReq, user: str = UserToken):
    cid = db.run("INSERT INTO comments(entry_id,file,line,line_to,side,body,user_token) VALUES(?,?,?,?,?,?,?)",
                 (entry_id, req.file, req.line, req.line_to, req.side, req.body, user))
    return db.one("SELECT * FROM comments WHERE id=?", (cid,))


@app.delete("/api/comments/{comment_id}")
def del_comment(comment_id: int, user: str = UserToken):
    db.run("DELETE FROM comments WHERE id=? AND user_token=?", (comment_id, user))
    return {"ok": True}


class ComposeReq(BaseModel):
    summary: str = ""


@app.post("/api/entries/{entry_id}/compose")
def compose(entry_id: int, req: ComposeReq, user: str = UserToken):
    st = db.entry_state(entry_id, user)   # composes only this user's comments
    if not st:
        raise HTTPException(404)
    e, s = st["entry"], st["series"] or {}
    files = analyze.parse_diff(st.get("diff") or "")
    parts = []
    if req.summary.strip():
        parts.append(req.summary.strip())
    for c in st["comments"]:
        if c["side"] == "rows":
            # mixed selection: line/line_to are physical row indexes into the file's hunks
            rows = [ln for f2 in files if f2["path"] == c["file"]
                    for h in f2["hunks"] for ln in h["lines"]]
            seg = rows[c["line"]:(c.get("line_to") or c["line"]) + 1][:30]
            ctx = [(tag if tag != " " else " ") + text for tag, _o, _n, text in seg]
            news = [n for _t, _o, n, _x in seg if n is not None]
            rng = f"{min(news)}–{max(news)}" if news else "?"
            label = f"{c['file']}:{rng} (selection includes removed lines)"
        else:
            ctx = _context_lines(files, c["file"], c["line"], side=c["side"],
                                 line_to=c.get("line_to"))
            rng = f"{c['line']}" + (f"–{c['line_to']}" if c.get("line_to") else "")
            label = f"{c['file']}:{rng}" + (" (removed line)" if c["side"] == "old" else "")
        parts.append(label + "\n" + "\n".join("> " + l for l in ctx)
                     + f"\n\n{c['body']}")
    body = "\n\n\n".join(parts) or "(no comments)"
    last = st["messages"][-1] if st["messages"] else {}
    subject = f"Re: {e['title']}"
    headers = {"To": "pgsql-hackers@lists.postgresql.org", "Subject": subject,
               "In-Reply-To": f"<{last.get('msgid', s.get('msgid') or '')}>",
               "References": f"<{last.get('msgid', s.get('msgid') or '')}>"}
    email_text = "\n".join(f"{k}: {v}" for k, v in headers.items()) + "\n\n" + body
    mailto = ("mailto:pgsql-hackers@lists.postgresql.org?"
              + urllib.parse.urlencode({"subject": subject, "body": body},
                                       quote_via=urllib.parse.quote))
    return {"email_text": email_text, "mailto": mailto}


def _context_lines(files, path, line, side="new", n=4, line_to=None):
    idx = 1 if side == "old" else 2   # parse_diff line tuples: (tag, old_ln, new_ln, text)
    for f in files:
        if f["path"] != path:
            continue
        for h in f["hunks"]:
            lines = [(ln[idx], ln[3]) for ln in h["lines"] if ln[idx] is not None]
            if lines and lines[0][0] <= line <= lines[-1][0]:
                if line_to:   # range comment: quote the selected lines (capped)
                    return [t for num, t in lines if line <= num <= line_to][:20]
                keep = [t for num, t in lines if line - n < num <= line]
                return keep[-n:]
    return []


class ChatReq(BaseModel):
    messages: list[dict]   # [{role: "user"|"assistant", content: str}, ...]


@app.post("/api/entries/{entry_id}/chat")
def chat(entry_id: int, req: ChatReq, user: str = UserToken):
    st = db.entry_state(entry_id, user)
    if not st:
        raise HTTPException(404)
    e, a = st["entry"], st.get("analysis") or {}
    convo = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in req.messages[-12:])
    prompt = f"""You are PatchLens chat, helping a reviewer understand a PostgreSQL commitfest patch.

Patch: {e['title']} (status: {e['status']}, target: PostgreSQL {e.get('target_version') or '?'})

Analysis overview:
{a.get('overview_md') or '(not available)'}

Full applied diff:
```
{(st.get('diff') or '')[:analyze.MAX_DIFF_CHARS]}
```

Conversation so far:
{convo}

Answer the last user message. Be concise and concrete; reference functions and
line numbers from the diff where relevant. Markdown."""
    reply = analyze._llm_raw(prompt).strip()
    # persist the exchange so it survives refreshes and stays private to this token
    if req.messages and req.messages[-1].get("role") == "user":
        db.run("INSERT INTO chat_messages(entry_id,user_token,role,content) VALUES(?,?,?,?)",
               (entry_id, user, "user", req.messages[-1]["content"]))
    db.run("INSERT INTO chat_messages(entry_id,user_token,role,content) VALUES(?,?,?,?)",
           (entry_id, user, "assistant", reply))
    return {"reply": reply}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
