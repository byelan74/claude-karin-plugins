#!/usr/bin/env python3
"""claude-recall — search your past Claude Code sessions.

    recall.py search "deploy script" [--project X] [--since 2026-09-01] [--limit 8]
    recall.py sessions [--project X] [--since …] [--grep …]
    recall.py show <session_id> [--last | --line N] [--context 6] [--all]
    recall.py forget <session_id> [--dry-run]
    recall.py verify [--sample 20]
    recall.py stats

Design notes that matter when reading the output:

  * results are COLLAPSED TO ONE ROW PER SESSION on purpose: one busy project can own most
    of the sessions, and without collapsing it buries everything else.
  * ranking is recency-first: every match is scored, none are pre-cut. bm25 over a trigram
    index measures substring frequency rather than term relevance, so it only breaks ties.
  * a query that matches nothing falls back — drop the longest term, then OR — because a
    Thai clause has no spaces and would otherwise be one unmatchable exact substring.
  * `show` reads from the database, not the .jsonl, so it still works after Claude Code's
    30-day cleanup has deleted the original. --raw goes to the .jsonl when it still exists.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recall_lib as L  # noqa: E402

TERM_RE = re.compile(r"[^\s\"'()]+")
ALL_TERMS = "all terms"
HALF_LIFE_DAYS = 60.0


# ---------------------------------------------------------------- helpers

def _age_days(iso: str) -> float:
    if not iso:
        return 365.0
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 365.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - t).total_seconds() / 86400.0)


def _terms(query: str):
    """Terms from the user's query. A quoted "…" span stays one term (a phrase)."""
    out = []
    # ' opens a phrase only at the start or after a space: "Alice's laptop" and "don't"
    # used to become the fake phrase "s laptop don"
    for m in re.finditer(r"\"([^\"]+)\"|(?:^|(?<=\s))'([^']+)'|([^\s\"]+)", query):
        t = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if t:
            out.append(t)
    return out


def _phrase(t: str) -> str:
    return '"' + t.replace('"', '""') + '"'


def _strategies(query: str):
    """MATCH expressions to try in order, with a label for each.

    trigram indexes nothing shorter than 3 characters, so short terms are dropped from
    MATCH and applied as LIKE filters instead.

    The fallbacks exist because Thai has no spaces: a natural clause like
    "ทำไมถึงเลิกใช้ hidden bar" becomes one long exact substring ANDed with the rest and
    matches nothing — the skill's own example returned 0 results before this.
    """
    long_t, short_t = [], []
    for t in _terms(query):
        (long_t if len(t) >= 3 else short_t).append(t)
    out = []
    if long_t:
        out.append((" AND ".join(_phrase(t) for t in long_t), ALL_TERMS))
        if len(long_t) > 1:
            trimmed = sorted(long_t, key=len)[:-1]
            out.append((" AND ".join(_phrase(t) for t in trimmed),
                        "dropped the longest term: " + " ".join(trimmed)))
            out.append((" OR ".join(_phrase(t) for t in long_t), "any one term"))
    return out, short_t, long_t


def _snippet(text: str, terms, width: int = 150) -> str:
    low = text.lower()
    pos = -1
    for t in terms:
        p = low.find(t.lower())
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        pos = 0
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    out = text[start:end].replace("\n", " ⏎ ")
    return ("…" if start else "") + out.strip() + ("…" if end < len(text) else "")


def _fmt_date(iso: str) -> str:
    return L.local_date(iso)


def _short(sid: str) -> str:
    return sid.split("-")[0]


def _resolve_session(conn, needle: str):
    """Accept a full session id or any unique prefix."""
    rows = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = ? OR session_id LIKE ?",
        (needle, needle + "%"),
    ).fetchall()
    if not rows:
        raise SystemExit(f"no session matching {needle!r}")
    if len(rows) > 1:
        ids = ", ".join(_short(r["session_id"]) for r in rows[:6])
        raise SystemExit(f"{needle!r} is ambiguous: {ids}")
    return rows[0]["session_id"]


def _auto_extract(quiet: bool = True) -> None:
    """Keep the index fresh. Cheap: a no-change incremental pass is well under a second.

    The very first run indexes the whole archive and can take a while on a big one, so the
    timeout is generous; after that each refresh only reads what was appended.
    """
    try:
        subprocess.run(
            [sys.executable, str(L.RECALL_DIR / "extract.py"), "--quiet",
             "--embed-cap", "64" if L.EMBED_ENABLED else "0", "--embed-timeout", "4"],
            check=False, timeout=180,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # a stale index is still useful; never block a search on the indexer


# ---------------------------------------------------------------- search

def _search_memories(conn, a, strategies, short_terms, terms):
    """Memory files that match, best first. Same MATCH fallback chain as turns.

    No recency weighting and no --since: a memory file is a distilled rule, true until
    someone edits or deletes it, so its age says nothing about whether it still holds.
    A term in the name/description outranks one buried in the body — the description is
    the one-line claim the file exists to make.
    """
    if a.memories <= 0:
        return [], None
    for expr, label in (strategies or []):
        w, p = [], []
        for t in short_terms:
            w.append("(m.body LIKE ? OR m.name LIKE ? OR m.description LIKE ?)")
            p += [f"%{t}%"] * 3
        if a.project:
            w.append("m.project LIKE ?")
            p.append(f"%{a.project}%")
        sql = ("SELECT m.*, bm25(memories_fts) AS bm FROM memories_fts"
               " JOIN memories m ON m.mem_id = memories_fts.rowid"
               " WHERE memories_fts MATCH ?")
        if w:
            sql += " AND " + " AND ".join(w)
        rows = conn.execute(sql, [expr] + p).fetchall()
        if rows:
            def key(r):
                head = f"{r['name']} {r['description']}".lower()
                # the date is printed with each hit, so a newer file that corrects an older
                # one is visible; mtime only breaks exact ties
                return (-sum(1 for t in terms if t.lower() in head), r["bm"], -(r["mtime"] or 0))
            return sorted(rows, key=key)[: a.memories], label
    return [], None


# Semantic supplement (optional, RECALL_EMBED_URL). Threshold measured with bge-m3 on a real
# archive of ~12k vectors:
#   queries with a real answer   top-1 cos 0.61 – 0.73  (Thai↔English and paraphrase)
#   off-topic queries            top-1 cos 0.43 – 0.58
# 0.60 sits in that gap, but the margin is ~0.02 — expect some false positives, which is
# why the section is labelled "check before trusting" and never merged into the ranking.
# Another embedding model needs its own threshold.
SEM_THRESHOLD = 0.60
SEM_MEM_THRESHOLD = 0.60
SEM_SESSIONS = 3
SEM_MEMORIES = 3
SEM_QUERY_TIMEOUT = 4.0


def _semantic(conn, a, exclude_sessions, exclude_mems):
    """Turns/memories CLOSE IN MEANING but missed by the keyword pass.

    Not a fusion ranker: keyword ranking stays exactly as it was, and this only adds what
    it could not find because the words differ (e.g. a Thai word vs its English term).
    Returns (sessions, memories, note). Never raises; silent when not configured.
    """
    if a.no_semantic or not L.EMBED_ENABLED:
        return [], [], None
    try:
        q = L.embed([a.query], SEM_QUERY_TIMEOUT)[0]
    except Exception as exc:
        return [], [], (f"no meaning-based results (embedding server did not answer: "
                        f"{type(exc).__name__} — if it just started, retry; models take a "
                        "while to load)")
    w, p = ["t.is_sidechain = 0"], []
    if not a.headless:
        w.append("COALESCE(s.entrypoint,'') <> 'sdk-cli'")
    if a.project:
        w.append("s.project LIKE ?")
        p.append(f"%{a.project}%")
    if a.since:
        w.append("t.ts >= ?")
        p.append(a.since)
    best: dict = {}
    for r in conn.execute(
            "SELECT v.turn_id, v.vec, t.session_id FROM turn_vecs v"
            " JOIN turns t ON t.turn_id = v.turn_id JOIN sessions s ON s.session_id = t.session_id"
            " WHERE " + " AND ".join(w), p):
        if r["session_id"] in exclude_sessions:
            continue
        sim = L.dot(q, L.unpack_vec(r["vec"]))
        if sim >= SEM_THRESHOLD and sim > best.get(r["session_id"], (0, 0))[0]:
            best[r["session_id"]] = (sim, r["turn_id"])
    sess = sorted(best.items(), key=lambda kv: -kv[1][0])[:SEM_SESSIONS]

    mems = []
    mw, mp = ["vec IS NOT NULL"], []
    if a.project:
        mw.append("project LIKE ?")
        mp.append(f"%{a.project}%")
    if a.memories > 0:
        for r in conn.execute("SELECT * FROM memories WHERE " + " AND ".join(mw), mp):
            if r["path"] in exclude_mems:
                continue
            sim = L.dot(q, L.unpack_vec(r["vec"]))
            if sim >= SEM_MEM_THRESHOLD:
                mems.append((sim, r))
        mems = sorted(mems, key=lambda x: -x[0])[:SEM_MEMORIES]
    return sess, mems, None


def cmd_search(conn, a) -> int:
    strategies, short_terms, long_terms = _strategies(a.query)
    terms = _terms(a.query)
    if not strategies and not short_terms:
        raise SystemExit('give search terms, e.g.: recall.py search "deploy script"')

    def _filters():
        w, p = [], []
        if not a.sidechain:
            w.append("t.is_sidechain = 0")
        if not a.headless:
            w.append("t.session_id NOT IN (SELECT session_id FROM sessions"
                     " WHERE entrypoint = 'sdk-cli')")
        for t in short_terms:
            w.append("t.text LIKE ?")
            p.append(f"%{t}%")
        if a.project:
            w.append("t.session_id IN (SELECT session_id FROM sessions"
                     " WHERE project LIKE ?)")
            p.append(f"%{a.project}%")
        if a.since:
            w.append("t.ts >= ?")
            p.append(a.since)
        return w, p

    # Score EVERY match, then cut. The previous version did `ORDER BY bm25 LIMIT 800`
    # first: for a common term that threw away most of the recent turns before recency was
    # ever applied, and because bm25 over trigram favours short documents it preferred
    # one-line turns (kept avg 2.2 KB, dropped avg 5.1 KB) — i.e. it discarded exactly the
    # long explanatory answers a person is looking for. Only ids+timestamps are fetched
    # here, so even a 2,000-hit term costs nothing; the text comes later for the few shown.
    mems, mem_used = _search_memories(conn, a, strategies, short_terms, terms)

    hits, used = [], None
    for expr, label in (strategies or [(None, "")]):
        w, params = _filters()
        if expr is not None:
            sql = ("SELECT t.turn_id, t.session_id, t.ts, bm25(turns_fts) AS bm"
                   " FROM turns_fts JOIN turns t ON t.turn_id = turns_fts.rowid"
                   " WHERE turns_fts MATCH ?")
            params = [expr] + params
        else:
            sql = ("SELECT t.turn_id, t.session_id, t.ts, 0.0 AS bm FROM turns t"
                   " WHERE 1=1")
        if w:
            sql += " AND " + " AND ".join(w)
        hits = conn.execute(sql, params).fetchall()
        if hits:
            used = label
            break

    kw_sessions = set()
    if hits:
        kw_sessions = {h["session_id"] for h in hits}
    sem_sess, sem_mems, sem_note = _semantic(conn, a, kw_sessions, {m["path"] for m in mems})

    if not hits and not mems and not sem_sess and not sem_mems:
        if a.json:
            print(json.dumps({"query": a.query, "memories": [], "results": [],
                              "semantic": [], "semantic_note": sem_note}, ensure_ascii=False))
        else:
            print(f"no match: {a.query!r}")
            if sem_note:
                print(sem_note)
            print("try fewer/shorter terms, or: recall.py sessions --grep <word> (searches titles)")
        return 1

    def _mem_row(m, sim=None):
        return {"name": m["name"], "description": m["description"], "type": m["mtype"],
                "project": m["project"], "path": m["path"],
                "modified": datetime.fromtimestamp(m["mtime"] or 0, L.LOCAL_TZ).strftime("%Y-%m-%d"),
                "snippet": _snippet(m["body"], terms),
                "match": "keyword" if sim is None else f"meaning cos={sim:.2f}"}
    sem_out = []
    for sid, (sim, tid) in sem_sess:
        s_ = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        t_ = conn.execute("SELECT role, ts, line_no, text FROM turns WHERE turn_id=?",
                          (tid,)).fetchone()
        if s_ is None or t_ is None:
            continue
        sem_out.append({"session_id": sid, "project": s_["project"],
                        "title": s_["title"] or (s_["first_user_msg"] or "")[:80] or None,
                        "date": _fmt_date(t_["ts"]), "cos": round(sim, 3),
                        "role": t_["role"], "line": t_["line_no"],
                        "snippet": _snippet(t_["text"], terms)})
    mem_out = [_mem_row(m) for m in mems] + [_mem_row(m, sim) for sim, m in sem_mems]

    by_session: dict = {}
    for h in hits:
        base = -float(h["bm"]) if h["bm"] else 1.0
        # Recency is measured on the MATCHING TURN, not the session: sessions here get
        # --resumed for weeks (one runs 4 Aug -> 20 Sep), so the session's own last
        # activity would make a six-week-old answer look like today's.
        weighted = base / (1 + _age_days(h["ts"]) / HALF_LIFE_DAYS)
        by_session.setdefault(h["session_id"], []).append((weighted, h["turn_id"]))

    scored = []
    for sid, rows in by_session.items():
        s = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if s is None:
            continue
        rows.sort(key=lambda r: -r[0])
        score = rows[0][0] * (1 + 0.15 * math.log1p(len(rows)))
        scored.append((score, s, [tid for _, tid in rows[:a.per_session]], len(rows)))
    scored.sort(key=lambda x: -x[0])
    scored = scored[: a.limit]

    want = [tid for _, _, tids, _ in scored for tid in tids]
    qmarks = ",".join("?" * len(want))
    turns = {r["turn_id"]: r for r in conn.execute(
        f"SELECT turn_id, role, ts, line_no, text FROM turns WHERE turn_id IN ({qmarks})",
        want)} if want else {}

    if a.json:
        out = []
        for score, s, tids, n in scored:
            rows = [turns[t] for t in tids if t in turns]
            out.append({
                "session_id": s["session_id"],
                "title": s["title"] or (s["first_user_msg"] or "")[:80] or None,
                "project": s["project"],
                "date": _fmt_date(rows[0]["ts"]) if rows else _fmt_date(s["ended_at"]),
                "session_span": f"{_fmt_date(s['started_at'])}→{_fmt_date(s['ended_at'])}",
                "hits": n, "score": round(score, 3),
                "jsonl_exists": bool(s["jsonl_exists"]),
                "matches": [{"role": r["role"], "ts": r["ts"], "line": r["line_no"],
                             "snippet": _snippet(r["text"], terms)} for r in rows],
            })
        print(json.dumps({"query": a.query, "strategy": used,
                          "memory_strategy": mem_used, "memories": mem_out,
                          "results": out, "semantic": sem_out, "semantic_note": sem_note},
                         ensure_ascii=False, indent=2))
        return 0

    if mem_out:
        mnote = (f"  (not every term matched — used: {mem_used})"
                 if mem_used and mem_used != ALL_TERMS else "")
        print(f"📌 memory: {len(mem_out)} file(s) — distilled notes, trust these before raw chat{mnote}\n")
        for i, m in enumerate(mem_out, 1):
            tag = "" if m["match"] == "keyword" else f"  🔎 close in meaning {m['match'].split()[-1]}"
            print(f"[m{i}] {m['modified']} · {m['project']} · {m['name']}"
                  + (f" ({m['type']})" if m["type"] else "") + tag)
            if m["description"]:
                print(f"      {m['description'][:160]}")
            print(f"      {m['snippet']}")
            print(f"    → {m['path']}\n")

    note = f"  (not every term matched — used: {used})" if used and used != ALL_TERMS else ""
    print(f"{len(scored)} sessions · query {a.query!r}{note}\n")
    for i, (score, s, tids, n) in enumerate(scored, 1):
        rows = [turns[t] for t in tids if t in turns]
        title = s["title"] or (s["first_user_msg"] or "")[:60] or "(untitled)"
        gone = "" if s["jsonl_exists"] else "  [original .jsonl deleted — kept only in the index]"
        span = ""
        if _fmt_date(s["started_at"]) != _fmt_date(s["ended_at"]):
            span = f"  (session {_fmt_date(s['started_at'])}→{_fmt_date(s['ended_at'])})"
        head = _fmt_date(rows[0]["ts"]) if rows else _fmt_date(s["ended_at"])
        print(f"[{i}] {head} · {s['project']} · {title}{gone}")
        print(f"    {_short(s['session_id'])} · {n} hits{span}")
        for r in rows:
            who = "you" if r["role"] == "user" else "claude"
            print(f"      {who:6} {L.local_stamp(r['ts'], 16)}  {_snippet(r['text'], terms)}")
        if rows:
            print(f"    → recall.py show {_short(s['session_id'])} --line {rows[0]['line_no']}\n")
    if sem_out:
        print(f"🔎 close in meaning (different words) {len(sem_out)} sessions — check before trusting\n")
        for i, r in enumerate(sem_out, 1):
            who = "you" if r["role"] == "user" else "claude"
            print(f"[s{i}] {r['date']} · {r['project']} · {r['title'] or '(untitled)'}  cos={r['cos']:.2f}")
            print(f"      {who:6} {r['snippet']}")
            print(f"    → recall.py show {_short(r['session_id'])} --line {r['line']}\n")
    if sem_note:
        print(sem_note)
    return 0


# ---------------------------------------------------------------- catalog

def cmd_sessions(conn, a) -> int:
    where, params = [], []
    if not a.headless:
        where.append("COALESCE(entrypoint,'') <> 'sdk-cli'")
    if a.project:
        where.append("project LIKE ?")
        params.append(f"%{a.project}%")
    if a.since:
        where.append("ended_at >= ?")
        params.append(a.since)
    if a.grep:
        where.append("(title LIKE ? OR first_user_msg LIKE ?)")
        params += [f"%{a.grep}%", f"%{a.grep}%"]
    sql = "SELECT * FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ended_at DESC LIMIT ?"
    params.append(a.limit)
    rows = conn.execute(sql, params).fetchall()

    if a.json:
        print(json.dumps([{k: r[k] for k in r.keys()} for r in rows],
                         ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("no sessions match")
        return 1
    print(f"{len(rows)} sessions\n")
    for r in rows:
        title = r["title"] or (r["first_user_msg"] or "")[:60] or "(untitled)"
        gone = "" if r["jsonl_exists"] else " [archived]"
        print(f"{_fmt_date(r['ended_at'])}  {_short(r['session_id'])}  "
              f"{r['project'][:22]:22}  {r['n_user']:>4}u/{r['n_assistant']:<4}a  "
              f"{title[:60]}{gone}")
    return 0


def cmd_show(conn, a) -> int:
    sid = _resolve_session(conn, a.session)
    s = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    print(f"# {s['title'] or '(untitled)'}")
    print(f"  {s['project']} · {_fmt_date(s['started_at'])} → {_fmt_date(s['ended_at'])}"
          f" · {s['n_user']}u/{s['n_assistant']}a · {sid}")
    print(f"  cwd {s['cwd']}")
    print(f"  jsonl {'present' if s['jsonl_exists'] else 'deleted'}: {s['jsonl_path']}\n")

    if a.raw:
        if not s["jsonl_exists"]:
            print("the original is gone — use normal mode (reads from the index)")
            return 1
        print(f"original: {s['jsonl_path']}")
        return 0

    # Order by TIME, not by seq. `seq` restarts per source file, so a session that was
    # --resumed into a second .jsonl had its newest turn sorting between the first file's
    # turns — which showed the conversation out of order and made --last land in the
    # middle of it. Undated turns keep their file/seq order at the end.
    rows = conn.execute(
        "SELECT * FROM turns WHERE session_id=?"
        " ORDER BY (ts IS NULL OR ts=''), ts, file_id, seq", (sid,)
    ).fetchall()
    if not rows:
        print("(no indexed text in this session)")
        return 1
    # Subagent turns share the session_id but carry line numbers from their own
    # subagents/agent-*.jsonl, so `--line N` (a pointer from `search`, which hides
    # subagents) used to land on a subagent turn with the same number. Hide them unless
    # asked, the same default as `search`.
    if not a.sidechain:
        main = [r for r in rows if not r["is_sidechain"]]
        if main:
            rows = main
    window = a.context * 2 + 1
    if a.all:
        pass
    elif a.last:
        # "where did this session get to" — the common question for a session that is
        # still running. Without it the only way to reach the end was --line 999999.
        rows = rows[-window:]
    elif a.line is not None:
        idx = min(range(len(rows)), key=lambda i: abs((rows[i]["line_no"] or 0) - a.line))
        lo, hi = max(0, idx - a.context), min(len(rows), idx + a.context + 1)
        rows = rows[lo:hi]
    else:
        rows = rows[:window]

    for r in rows:
        who = "YOU" if r["role"] == "user" else "CLAUDE"
        side = " (subagent)" if r["is_sidechain"] else ""
        print(f"--- {who}{side}  {L.local_stamp(r['ts'], 19)}  line {r['line_no']}")
        text = r["text"]
        if not a.all and len(text) > a.maxchars:
            text = text[: a.maxchars] + f"\n… [cut at {a.maxchars} chars — use --all for the full text]"
        print(text + "\n")

    files = conn.execute(
        "SELECT kind, value, n FROM touches WHERE session_id=? AND kind='file'"
        " ORDER BY n DESC LIMIT 12", (sid,)).fetchall()
    if files:
        print("files touched in this session:")
        for f in files:
            print(f"   {f['n']:>3}x  {f['value']}")
    return 0


# ---------------------------------------------------------------- maintenance

def cmd_forget(conn, a) -> int:
    # wait for any running extract, else it can re-index this session mid-scan
    _lock = L.acquire_lock(L.DB_PATH, blocking=True)  # noqa: F841 (held until return)
    sid = _resolve_session(conn, a.session)
    s = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    n_t = conn.execute("SELECT COUNT(*) FROM turns WHERE session_id=?", (sid,)).fetchone()[0]
    n_c = conn.execute("SELECT COUNT(*) FROM touches WHERE session_id=?", (sid,)).fetchone()[0]
    print(f"{sid}\n  {s['project']} · {_fmt_date(s['ended_at'])} · {s['title'] or '(untitled)'}")
    print(f"  will delete: {n_t} turns, {n_c} touches, 1 catalogue row")
    if a.dry_run:
        print("  (dry run — nothing deleted)")
        return 0

    srcs = [r["src_path"] for r in conn.execute(
        "SELECT DISTINCT src_path FROM session_files WHERE session_id=?", (sid,))]
    conn.execute("DELETE FROM turn_vecs WHERE turn_id IN"
                 " (SELECT turn_id FROM turns WHERE session_id=?)", (sid,))
    conn.execute("DELETE FROM turns_fts WHERE rowid IN"
                 " (SELECT turn_id FROM turns WHERE session_id=?)", (sid,))
    conn.execute("DELETE FROM touches_fts WHERE rowid IN"
                 " (SELECT touch_id FROM touches WHERE session_id=?)", (sid,))
    conn.execute("DELETE FROM turns WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM touches WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM session_files WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE session_id=?", (sid,))
    # Without this the next incremental scan would happily re-index it from its offset.
    for p in srcs:
        conn.execute("DELETE FROM files_seen WHERE path=?", (p,))
        conn.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (f"forgotten:{p}", "1"))
    conn.commit()
    print("  deleted · the original transcript is untouched and will not be re-indexed")
    print("  (extract skips these files for as long as they exist)")
    for p in srcs:
        print(f"      {p}")
    return 0


def cmd_verify(conn, a) -> int:
    """Prove the index matches reality instead of asserting it."""
    import random
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    on_disk = len(L.transcript_files())
    archived = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE jsonl_exists=0").fetchone()[0]
    print(f"sessions in index : {total}")
    print(f".jsonl on disk    : {on_disk}")
    print(f"archived (original gone, text kept in the index): {archived}")

    fts_t = conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0]
    tur_t = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    fts_c = conn.execute("SELECT COUNT(*) FROM touches_fts").fetchone()[0]
    tou_c = conn.execute("SELECT COUNT(*) FROM touches").fetchone()[0]
    print(f"turns {tur_t} / fts {fts_t}    touches {tou_c} / fts {fts_c}")
    problems = []
    if fts_t != tur_t:
        problems.append(f"turns_fts does not match turns ({fts_t} vs {tur_t})")
    if fts_c != tou_c:
        problems.append(f"touches_fts does not match touches ({fts_c} vs {tou_c})")

    orphan = conn.execute(
        "SELECT COUNT(*) FROM turns WHERE session_id NOT IN (SELECT session_id FROM sessions)"
    ).fetchone()[0]
    if orphan:
        problems.append(f"{orphan} turns have no session in the catalogue")

    mismatch = conn.execute(
        "SELECT COUNT(*) FROM sessions s WHERE s.n_user <>"
        " (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.session_id AND t.role='user')"
    ).fetchone()[0]
    if mismatch:
        problems.append(f"{mismatch} sessions whose n_user does not match turns")

    # Round-trip check: re-read the recorded line, run the SAME extraction the indexer
    # runs, and require the result to be identical to what is stored. An earlier version
    # fell back to "the line contains this session id", which every line does — it passed
    # 25/25 while proving nothing.
    rows = conn.execute(
        "SELECT t.turn_id, f.path AS src_path, t.line_no, t.text FROM turns t"
        " JOIN sessions s ON s.session_id=t.session_id"
        " JOIN files f ON f.file_id=t.file_id"
        " WHERE s.jsonl_exists=1 AND LENGTH(t.text) > 40").fetchall()
    sample = random.sample(rows, min(a.sample, len(rows))) if rows else []
    checked = ok = 0
    bad_examples = []
    for r in sample:
        line = None
        try:
            with open(r["src_path"], encoding="utf-8", errors="replace") as fh:
                for i, raw in enumerate(fh, 1):
                    if i == r["line_no"]:
                        line = raw
                        break
        except OSError:
            continue
        if line is None:
            continue
        checked += 1
        try:
            d = json.loads(line)
        except ValueError:
            bad_examples.append(f"line {r['line_no']} is not JSON")
            continue
        again = L.clean_text(L.blocks_text((d.get("message") or {}).get("content")))
        if again == r["text"]:
            ok += 1
        else:
            bad_examples.append(
                f"turn {r['turn_id']} ({Path(r['src_path']).name}:{r['line_no']})")
    print(f"round-trip re-extract vs original .jsonl: {ok}/{checked} identical")
    if checked and ok < checked:
        problems.append(f"round-trip mismatch {checked - ok} of {checked}: "
                        + "; ".join(bad_examples[:3]))

    n_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    n_mem_fts = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
    n_mem_disk = len(L.memory_files())
    print(f"memory files: disk {n_mem_disk} / index {n_mem} / fts {n_mem_fts}")
    if n_mem != n_mem_fts:
        problems.append(f"memories_fts does not match memories ({n_mem_fts} vs {n_mem})")
    if n_mem != n_mem_disk:
        problems.append(f"{n_mem_disk} memory files on disk but {n_mem} indexed — not synced yet")

    # format drift (see recall_lib.KNOWN_*). Unknown record types are common and harmless
    # unless they carry message content; an unknown BLOCK type is how text or tool output
    # would arrive unseen, so that always counts.
    def _meta(k):
        r = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else {}
    new_types = sorted(k for k in _meta("seen_types") if k not in L.KNOWN_RECORD_TYPES)
    new_blocks = sorted(k for k in _meta("seen_blocks") if k not in L.KNOWN_BLOCK_TYPES)
    carriers = _meta("content_types")
    print(f"format: new record types {new_types or '-'} · new block types {new_blocks or '-'}")
    if new_blocks:
        problems.append(f"unknown block type {new_blocks} — text or tool output could arrive this way"
                        " · inspect, then add to KNOWN_BLOCK_TYPES")
    if carriers:
        problems.append(f"unknown record type carrying message.content {sorted(carriers)}"
                        " — if it is conversation, the extractor is dropping it")
    for label, where in L.CANARIES:
        n = conn.execute(f"SELECT COUNT(*) FROM turns WHERE {where}").fetchone()[0]
        if n:
            problems.append(f"canary '{label}': {n} turns — an exclusion flag may have been renamed")
    print("canary (compact summary · skill body · pipeline payload): "
          + ", ".join(str(conn.execute(f"SELECT COUNT(*) FROM turns WHERE {w}").fetchone()[0])
                      for _, w in L.CANARIES))
    recent_null = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE jsonl_exists=1 AND entrypoint IS NULL"
        " AND ended_at >= datetime('now','-7 days')").fetchone()[0]
    if recent_null:
        problems.append(f"{recent_null} recent sessions have no entrypoint — the field may have"
                        " been renamed (pipeline payloads would leak back in)")

    orphan_v = conn.execute("SELECT COUNT(*) FROM turn_vecs WHERE turn_id NOT IN"
                            " (SELECT turn_id FROM turns)").fetchone()[0]
    n_v = conn.execute("SELECT COUNT(*) FROM turn_vecs").fetchone()[0]
    n_mv = conn.execute("SELECT COUNT(*) FROM memories WHERE vec IS NOT NULL").fetchone()[0]
    print(f"vectors: turns {n_v} · memories {n_mv} of {n_mem} · orphan {orphan_v}")
    if orphan_v:
        problems.append(f"{orphan_v} vectors without a turn — forget/rescan left them behind")

    dups = conn.execute("SELECT COUNT(*) FROM (SELECT 1 FROM turns GROUP BY file_id, line_no,"
                        " role HAVING COUNT(*) > 1)").fetchone()[0]
    print(f"duplicate turns (file_id, line_no): {dups}")
    if dups:
        problems.append(f"{dups} duplicate turns — did two extracts run at once?")

    leaked = 0
    for (txt,) in conn.execute("SELECT text FROM turns UNION ALL"
                               " SELECT name || ' ' || description || ' ' || body"
                               " FROM memories UNION ALL SELECT value FROM touches"):
        if L.detector_hits(txt):
            leaked += 1
    print(f"secret detector over every turn + memory + touch: {leaked} leaked")
    if leaked:
        problems.append(f"{leaked} rows still carry a secret pattern")

    print()
    if problems:
        print("❌ problems found:")
        for p in problems:
            print("   -", p)
        return 1
    print("✅ everything consistent")
    return 0


def cmd_stats(conn, a) -> int:
    g = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    db = L.DB_PATH
    size = sum(os.path.getsize(str(db) + s) for s in ("", "-wal", "-shm")
               if os.path.exists(str(db) + s))
    print(f"index      {db}")
    print(f"size       {size/1e6:.1f} MB")
    n_pipe = g("SELECT COUNT(*) FROM sessions WHERE entrypoint='sdk-cli'")
    print(f"pipeline   {n_pipe}"
          "  (claude -p / SDK — hidden from search/sessions unless --headless)")
    print(f"sessions   {g('SELECT COUNT(*) FROM sessions')}"
          f"  (archived {g('SELECT COUNT(*) FROM sessions WHERE jsonl_exists=0')})")
    # LENGTH() counts characters; non-Latin scripts take several bytes in utf-8, so the byte
    # figure is the one that matches what extract.py reports.
    print(f"turns      {g('SELECT COUNT(*) FROM turns')}"
          f"  ({g('SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))),0) FROM turns')/1e6:.1f} MB text)")
    print(f"touches    {g('SELECT COUNT(*) FROM touches')}")
    print(f"vectors    {g('SELECT COUNT(*) FROM turn_vecs')} turns"
          f" + {g('SELECT COUNT(*) FROM memories WHERE vec IS NOT NULL')} memories"
          + ("" if L.EMBED_ENABLED else "  (semantic search off — RECALL_EMBED_URL not set)"))
    print(f"memories   {g('SELECT COUNT(*) FROM memories')}"
          f"  ({g('SELECT COUNT(DISTINCT project) FROM memories')} projects)")
    r = conn.execute("SELECT MIN(started_at), MAX(ended_at) FROM sessions").fetchone()
    print(f"span       {_fmt_date(r[0])} → {_fmt_date(r[1])}")
    last = conn.execute("SELECT v FROM meta WHERE k='last_scan'").fetchone()
    print(f"last scan  {last['v'] if last else '?'}")
    print("\nbusiest projects:")
    for r in conn.execute(
            "SELECT project, COUNT(*) c, MAX(ended_at) last FROM sessions"
            " GROUP BY project ORDER BY c DESC LIMIT 10"):
        print(f"   {r['c']:>4}  {(r['project'] or '?')[:34]:34} last {_fmt_date(r['last'])}")
    return 0


# ---------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description="search your past Claude Code sessions")
    ap.add_argument("--no-refresh", action="store_true",
                    help="skip the quick incremental index pass before searching")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--project"); p.add_argument("--since")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--per-session", type=int, default=3)
    p.add_argument("--sidechain", action="store_true", help="include subagent turns")
    p.add_argument("--headless", action="store_true",
                   help="include claude -p / SDK (pipeline) sessions")
    p.add_argument("--no-semantic", action="store_true",
                   help="keywords only, skip the embedding server")
    p.add_argument("--memories", type=int, default=5,
                   help="max memory files to show (0 = don't search memory)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("sessions")
    p.add_argument("--project"); p.add_argument("--since"); p.add_argument("--grep")
    p.add_argument("--headless", action="store_true", help="include pipeline sessions")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_sessions)

    p = sub.add_parser("show")
    p.add_argument("session")
    p.add_argument("--line", type=int)
    p.add_argument("--last", action="store_true",
                   help="the end of the session (most recent) instead of the start")
    p.add_argument("--context", type=int, default=6)
    p.add_argument("--maxchars", type=int, default=1500)
    p.add_argument("--all", action="store_true"); p.add_argument("--raw", action="store_true")
    p.add_argument("--sidechain", action="store_true",
                   help="include subagent turns (hidden by default, like search)")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("forget")
    p.add_argument("session"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_forget)

    p = sub.add_parser("verify")
    p.add_argument("--sample", type=int, default=20)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("stats")
    p.set_defaults(fn=cmd_stats)

    a = ap.parse_args()
    L.utf8_stdio()
    # show too: `show <sid> --last` after a compact must see the turns written moments ago
    if a.cmd in ("search", "sessions", "show") and not getattr(a, "no_refresh", False):
        _auto_extract()
    conn = L.connect()
    L.ensure_schema(conn)
    try:
        return a.fn(conn, a)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
