"""Diff parsing + LLM passes (group -> explain -> findings -> thread summary)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

MODEL = "claude-opus-4-8"
MAX_DIFF_CHARS = 80_000  # cap what we send per LLM call


# ---------- unified diff parsing ----------

def parse_diff(diff: str) -> list[dict]:
    files = []
    cur = None
    old_ln = new_ln = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            m = re.match(r'diff --git a/(.*) b/(.*)', line)
            cur = {"path": m.group(2), "old_path": m.group(1),
                   "additions": 0, "deletions": 0, "hunks": []}
            files.append(cur)
        elif cur is not None and line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
            old_ln, new_ln = int(m.group(1)), int(m.group(2))
            cur["hunks"].append({"header": line, "func": m.group(3).strip(),
                                 "new_start": new_ln, "lines": []})
        elif cur is not None and cur["hunks"] and line[:1] in ("+", "-", " ", "\\"):
            h = cur["hunks"][-1]
            tag = line[:1]
            if tag == "+":
                h["lines"].append(["+", None, new_ln, line[1:]])
                cur["additions"] += 1
                new_ln += 1
            elif tag == "-":
                h["lines"].append(["-", old_ln, None, line[1:]])
                cur["deletions"] += 1
                old_ln += 1
            elif tag == " ":
                h["lines"].append([" ", old_ln, new_ln, line[1:]])
                old_ln += 1
                new_ln += 1
    return files


# ---------- LLM plumbing ----------

def _backend() -> str:
    """api | claude | codex — override with PGPATCHLENS_LLM."""
    forced = os.environ.get("PGPATCHLENS_LLM")
    if forced:
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    for cli in ("claude", "codex"):
        if shutil.which(cli):
            return cli
    raise RuntimeError("no LLM backend: set ANTHROPIC_API_KEY, or install and log in to "
                       "the claude or codex CLI (subscription-powered, no key needed)")


def _llm_raw(prompt: str) -> str:
    be = _backend()
    if be == "api":
        import anthropic
        client = anthropic.Anthropic()
        with client.messages.stream(model=MODEL, max_tokens=16000,
                                    thinking={"type": "adaptive"},
                                    messages=[{"role": "user", "content": prompt}]) as s:
            msg = s.get_final_message()
        return next(b.text for b in msg.content if b.type == "text")
    if be == "claude":   # the user's Claude Code login — their subscription, no key
        r = subprocess.run(
            ["claude", "-p", "--model", MODEL, "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=600,
            cwd=tempfile.gettempdir(),
        )
        if r.returncode != 0:
            raise RuntimeError(f"claude -p failed: {r.stderr[:500]}")
        return r.stdout
    if be == "codex":    # the user's Codex login
        with tempfile.NamedTemporaryFile("r", suffix=".txt", delete=False) as out:
            outpath = out.name
        try:
            r = subprocess.run(
                ["codex", "exec", "--skip-git-repo-check", "-o", outpath, "-"],
                input=prompt, capture_output=True, text=True, timeout=600,
                cwd=tempfile.gettempdir(),
            )
            if r.returncode != 0:
                raise RuntimeError(f"codex exec failed: {r.stderr[:500]}")
            with open(outpath) as fh:
                return fh.read()
        finally:
            os.unlink(outpath)
    raise RuntimeError(f"unknown PGPATCHLENS_LLM backend: {be}")


def llm_json(prompt: str) -> dict:
    out = _llm_raw(prompt + "\n\nRespond with ONLY the JSON object, no prose, no code fences.")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError(f"no JSON in LLM output: {out[:300]}")
    return json.loads(m.group(0))


# ---------- passes ----------

def _skeleton(files: list[dict]) -> str:
    """File list + hunk headers only — keeps pass A small."""
    out = []
    for f in files:
        out.append(f"{f['path']}  (+{f['additions']} -{f['deletions']})")
        for h in f["hunks"]:
            out.append(f"  {h['header']}")
    return "\n".join(out)


def pass_a_group(entry: dict, files: list[dict]) -> dict:
    prompt = f"""You are PatchLens, reviewing a PostgreSQL commitfest patch series.

Patch: {entry['title']} (target: PostgreSQL {entry.get('target_version') or '?'})

Below is the file list with unified-diff hunk headers (function names included).
Group the hunks into logical changes ordered for review: infrastructure first,
then integration, then GUC/costing, then tests, then docs. 2-7 groups.
Also write a short overview of what the series does and how the pieces connect
(overview_md, markdown, <=200 words, may include a small flow sketch in a code
block) and a one-paragraph reviewer summary (summary_md: what to focus on
before this can move to Ready for Committer).

{_skeleton(files)}

JSON schema:
{{"overview_md": str, "summary_md": str,
  "groups": [{{"title": str, "files": [str], "kind": str}}]}}"""
    return llm_json(prompt)


def pass_b_explain(entry: dict, groups: list[dict], diff: str) -> list[dict]:
    gl = "\n".join(f"{i+1}. {g['title']} — files: {', '.join(g['files'])}"
                   for i, g in enumerate(groups))
    prompt = f"""You are PatchLens, explaining a PostgreSQL patch series to a reviewer.

Patch: {entry['title']}

The diff has been grouped into logical changes:
{gl}

For each group write explanation_md (2-4 sentences of markdown: what the change
does and why, referencing key functions) and optionally snippet (<=10 lines of
the most instructive code from the diff for that group, plain text, else null).

Full diff:
```
{diff[:MAX_DIFF_CHARS]}
```

JSON schema: {{"explanations": [{{"ordinal": int, "explanation_md": str, "snippet": str|null}}]}}
(ordinal is the 1-based group number above)"""
    return llm_json(prompt)["explanations"]


PG_HEURISTICS = """Apply PostgreSQL-specific review heuristics: buffer pin/unpin
discipline, memory-context lifetimes, work_mem accounting, GUC defaults and docs,
rescan/ExecReScan paths, error-path resource cleanup (PG_TRY/PG_CATCH), catalog
locking, WAL/recovery correctness, race conditions between backends, missing
regression-test coverage."""


def pass_c_findings(entry: dict, diff: str) -> list[dict]:
    prompt = f"""You are PatchLens, doing a correctness review of a PostgreSQL patch series.

Patch: {entry['title']}

{PG_HEURISTICS}

Report findings with severity Bug (definite defect), Investigate (plausible
problem needing a human look), or Informational (worth knowing, not a problem).
Anchor each to a file and NEW-file line number that appears as an added or
context line in the diff, and quote the exact anchored line in `anchor_quote`.
Report everything you find including uncertain items; 0 findings is acceptable
only if the diff is trivial.

Full diff:
```
{diff[:MAX_DIFF_CHARS]}
```

JSON schema: {{"findings": [{{"severity": "Bug"|"Investigate"|"Informational",
  "title": str, "detail": str, "file": str,
  "line_from": int, "line_to": int, "anchor_quote": str}}]}}"""
    return llm_json(prompt)["findings"]


def validate_findings(findings: list[dict], files: list[dict]) -> list[dict]:
    """Hallucination filter: anchor must land in a hunk of a real file."""
    spans = {}
    for f in files:
        spans[f["path"]] = [(h["new_start"],
                             max((ln[2] for ln in h["lines"] if ln[2]), default=h["new_start"]))
                            for h in f["hunks"]]
    ok = []
    for fd in findings:
        rngs = spans.get(fd.get("file"))
        if not rngs:
            continue
        lf = fd.get("line_from") or 0
        if any(lo - 5 <= lf <= hi + 5 for lo, hi in rngs):
            fd["fingerprint"] = hashlib.sha1(
                f"{fd['severity']}|{re.sub(r'\\W+', ' ', fd['title'].lower()).strip()}|{fd['file']}"
                .encode()).hexdigest()[:16]
            ok.append(fd)
    return ok


def pass_d_thread(entry: dict, messages: list[dict]) -> str:
    convo = "\n\n".join(f"[{m['author']} · {m['sent_at']}]\n{m['body'][:1500]}"
                        for m in messages[-30:])
    prompt = f"""Summarize this pgsql-hackers thread about the patch
"{entry['title']}" for a reviewer: current state of the discussion, open
questions, and what is blocking Ready for Committer. Markdown, <=150 words.

{convo}

JSON schema: {{"thread_summary_md": str}}"""
    return llm_json(prompt)["thread_summary_md"]


if __name__ == "__main__":
    d = """diff --git a/x.c b/x.c
index 111..222 100644
--- a/x.c
+++ b/x.c
@@ -10,3 +10,4 @@ int f(void)
 a
-b
+bb
+c
"""
    fs = parse_diff(d)
    assert fs[0]["path"] == "x.c" and fs[0]["additions"] == 2 and fs[0]["deletions"] == 1
    assert fs[0]["hunks"][0]["lines"][2] == ["+", None, 11, "bb"]
    good = validate_findings(
        [{"severity": "Bug", "title": "t", "file": "x.c", "line_from": 11},
         {"severity": "Bug", "title": "t", "file": "nope.c", "line_from": 1}], fs)
    assert len(good) == 1 and good[0]["fingerprint"]
    print("analyze ok")
