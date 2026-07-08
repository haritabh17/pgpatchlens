"""Ingestion: commitfest entry page, mailing-list thread, cfbot GitHub diff + CI."""
import html
import re
import urllib.parse

import httpx

UA = {"User-Agent": "pg_patchlens/0.1 (local dev; contact: none)"}
CFBOT_REPO = "https://api.github.com/repos/postgresql-cfbot/postgresql"


def normalize(text: str) -> int | None:
    """Entry URL, bare id, legacy /cf/N/M url, or postgr.es/m link -> patch id."""
    text = text.strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = re.search(r"commitfest\.postgresql\.org/(?:patch/(\d+)|\d+/(\d+))", text)
    if m:
        return int(m.group(1) or m.group(2))
    # postgr.es/m/<msgid> or a message-id url: resolve via the archives redirect
    m = re.search(r"(?:postgr\.es/m/|/message-id/(?:flat/)?)([^\s/?#]+)", text)
    if m:
        return _entry_from_msgid(m.group(1))
    return None


def _entry_from_msgid(msgid: str) -> int | None:
    # the flat page links back to the commitfest entry when one exists
    r = httpx.get(f"https://www.postgresql.org/message-id/flat/{urllib.parse.quote(msgid)}",
                  headers=UA, follow_redirects=True, timeout=30)
    m = re.search(r"commitfest\.postgresql\.org/patch/(\d+)", r.text)
    return int(m.group(1)) if m else None


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def fetch_entry(patch_id: int) -> dict:
    r = httpx.get(f"https://commitfest.postgresql.org/patch/{patch_id}/",
                  headers=UA, follow_redirects=True, timeout=30)
    r.raise_for_status()
    s = re.sub(r"\s+", " ", r.text)
    fields = {}
    for m in re.finditer(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", s):
        fields[_strip(m.group(1))] = m.group(2)
    msgids = re.findall(r'/message-id/(?:flat/)?([^"\'/]+@[^"\'/]+)', r.text)
    status = _strip(fields.get("Status", ""))
    # latest CF line looks like "PG20-Drafts (…): Needs review …" — keep the first clause
    m = re.search(r":\s*([A-Za-z ]+?)(?:  |$)", status)
    cf = re.search(r"^\s*([\w-]+)", status)
    m_ver = re.search(r"Patch version:\s*(v?\d+)", _strip(fields.get("Stats (from CFBot)", "")))
    return {
        "id": patch_id,
        "title": _strip(fields.get("Title", "")),
        "status": m.group(1).strip() if m else status[:40],
        "cf_id": cf.group(1) if cf else "",
        "target_version": _strip(fields.get("Target version", "")),
        "authors": _strip(fields.get("Authors", "")),
        "reviewers": _strip(fields.get("Reviewers", "")).replace("Become reviewer", "").strip() or "None",
        "thread_msgid": msgids[0] if msgids else None,
        "latest_version": m_ver.group(1) if m_ver else "v1",
    }


def fetch_thread(msgid: str) -> dict:
    """Flat archive page -> messages + attachments of the latest patch series."""
    r = httpx.get(f"https://www.postgresql.org/message-id/flat/{urllib.parse.quote(msgid)}",
                  headers=UA, follow_redirects=True, timeout=30)
    r.raise_for_status()
    text = r.text
    msgs = []
    # each message: header table with From/Date/Message-ID then message-content div
    blocks = re.split(r'<table class="table-sm table-responsive message-header"', text)[1:]
    for b in blocks:
        frm = re.search(r"From:</th>\s*<td>(.*?)</td>", b, re.S)
        date = re.search(r"Date:</th>\s*<td>(.*?)</td>", b, re.S)
        mid = re.search(r'Message-ID:</th>\s*<td><a href="/message-id/[^"]+">(.*?)</a>', b, re.S)
        body = re.search(r'<div class="message-content">(.*?)</div>', b, re.S)
        if not (frm and body):
            continue
        author = _strip(frm.group(1)).replace("(at)", "@").replace("(dot)", ".")
        raw = re.sub(r"<br\s*/?>", "\n", body.group(1))
        raw = re.sub(r"</p>", "\n\n", raw)
        msgs.append({
            "msgid": _strip(mid.group(1)) if mid else "",
            "author": author,
            "sent_at": _strip(date.group(1)) if date else "",
            "body": _strip_keep_newlines(raw)[:20000],  # bound only pathological inline-patch mails
        })
    atts = re.findall(r'href="(/message-id/attachment/\d+/([^"]+\.(?:patch|diff)(?:\.gz)?))"', text)
    # latest series: highest vNN prefix wins; unversioned files count as v0
    def ver(name):
        m = re.match(r"v(\d+)", name)
        return int(m.group(1)) if m else 0
    latest = max((ver(n) for _, n in atts), default=0)
    patches = sorted({(f"https://www.postgresql.org{u}", n) for u, n in atts if ver(n) == latest},
                     key=lambda t: t[1])
    return {"messages": msgs, "patches": [{"url": u, "filename": n} for u, n in patches],
            "version": f"v{latest}" if latest else "v1"}


def _strip_keep_newlines(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def fetch_diff(patch_id: int) -> dict:
    """cfbot mirror branch cf/<id>: merge-base + applied unified diff, no git needed."""
    base = f"{CFBOT_REPO}/compare/master...cf/{patch_id}"
    meta = httpx.get(base, headers={**UA, "Accept": "application/vnd.github+json"}, timeout=60)
    if meta.status_code == 404:
        return {"applies": False}
    meta.raise_for_status()
    j = meta.json()
    d = httpx.get(base, headers={**UA, "Accept": "application/vnd.github.diff"}, timeout=120)
    d.raise_for_status()
    return {
        "applies": True,
        "base_sha": j["merge_base_commit"]["sha"],
        "head_sha": j["commits"][-1]["sha"] if j.get("commits") else j["merge_base_commit"]["sha"],
        "diff": d.text,
    }


def fetch_ci(patch_id: int) -> list[dict]:
    r = httpx.get(f"{CFBOT_REPO}/commits/cf%2F{patch_id}/check-runs?per_page=100",
                  headers={**UA, "Accept": "application/vnd.github+json"}, timeout=60)
    if r.status_code != 200:
        return []
    out = []
    for cr in r.json().get("check_runs", []):
        status = cr["conclusion"] if cr["status"] == "completed" else cr["status"]
        out.append({"task": cr["name"], "status": status or "queued", "detail_url": cr["html_url"]})
    return out


if __name__ == "__main__":
    assert normalize("https://commitfest.postgresql.org/patch/5231") == 5231
    assert normalize("5231") == 5231
    assert normalize("https://commitfest.postgresql.org/49/5231/") == 5231
    e = fetch_entry(6091)
    assert e["title"] and e["thread_msgid"], e
    t = fetch_thread(e["thread_msgid"])
    assert t["messages"] and t["patches"], (len(t["messages"]), t["patches"])
    d = fetch_diff(6091)
    assert d["applies"] and "diff --git" in d["diff"]
    ci = fetch_ci(6091)
    assert ci
    print(f"ingest ok: {e['title']!r}, {len(t['messages'])} msgs, {t['version']}, "
          f"{len(d['diff'])}B diff, {len(ci)} ci runs")
