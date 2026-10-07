#!/usr/bin/env python3
"""Index Claude Code session transcripts into the recall database (see recall_lib.DATA_DIR).

    python3 extract.py            # incremental (normal)
    python3 extract.py --full     # re-read everything (keeps archived sessions)
    python3 extract.py --quiet    # one summary line — for hooks / scheduled runs

Incremental means: remember each file's byte offset and carry on from there. The .jsonl
files are append-only, so this is safe — with two guards that both bite in practice:

  * the last line may be half-written while a session is live. If it does not parse AND
    has no trailing newline, we stop WITHOUT advancing the offset. Advancing past it would
    lose that turn permanently, and it would look like nothing was wrong.
  * a file that shrank, or whose mtime went backwards, was rewritten rather than appended
    to — that file is re-scanned from zero and its old rows are deleted first.

`sessions` is derived at the end of the scan, never written during it: one session can
span several files (--resume writes a new .jsonl), and a per-file write would let the
second file clobber the first one's counts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recall_lib as L  # noqa: E402

# tool_use input keys worth keeping. Everything else in `input` is a payload and is
# dropped — that is the whole point of the design.
PATH_KEYS = ("file_path", "path", "notebook_path")
CMD_MAX = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tool_uses(content):
    if not isinstance(content, list):
        return
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            yield b.get("name") or "?", b.get("input") or {}


def _drop_file_rows(conn, path: str) -> None:
    fid = L.file_id(conn, path)
    conn.execute("DELETE FROM turn_vecs WHERE turn_id IN (SELECT turn_id FROM turns"
                 " WHERE file_id=?)", (fid,))
    conn.execute(
        "DELETE FROM turns_fts WHERE rowid IN (SELECT turn_id FROM turns WHERE file_id=?)",
        (fid,),
    )
    conn.execute(
        "DELETE FROM touches_fts WHERE rowid IN"
        " (SELECT touch_id FROM touches WHERE file_id=?)", (fid,),
    )
    conn.execute("DELETE FROM turns WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM touches WHERE file_id=?", (fid,))
    conn.execute("DELETE FROM session_files WHERE src_path=?", (path,))


def scan_file(conn, path: str, full: bool, stats: dict) -> None:
    size = os.path.getsize(path)
    mtime = os.path.getmtime(path)
    row = conn.execute(
        "SELECT size, mtime, offset, line_no FROM files_seen WHERE path=?", (path,)
    ).fetchone()

    restart = full or row is None or row["size"] > size or row["mtime"] > mtime + 1.0
    if restart:
        _drop_file_rows(conn, path)
        start_off, start_line = 0, 0
        if row is not None and not full:
            stats["rescanned"] += 1
    else:
        start_off, start_line = row["offset"], row["line_no"]
        if start_off >= size:
            return  # nothing appended

    fid = L.file_id(conn, path)

    # per-file accumulators
    sid_seen = None
    meta: dict = {}
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0) FROM turns WHERE file_id=?", (fid,)
    ).fetchone()[0]

    pos, line_no = start_off, start_line
    with open(path, "rb") as fh:
        fh.seek(pos)
        while True:
            raw = fh.readline()
            if not raw:
                break
            complete = raw.endswith(b"\n")
            if not complete:
                # No trailing newline means the writer is mid-line. Stop here WITHOUT
                # advancing the offset, whether or not it happens to parse: consuming a
                # complete-but-unterminated line leaves the lone "\n" for the next run,
                # which then counts as a bad line and shifts every later line_no by one.
                stats["partial_tail"] += 1
                break
            try:
                d = json.loads(raw)
                ok = True
            except Exception:
                ok = False
            line_no += 1
            pos += len(raw)
            if not ok:
                stats["bad_lines"] += 1
                continue

            t = d.get("type")
            stats["seen_types"][t] = stats["seen_types"].get(t, 0) + 1
            _c = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
            if _c and t not in L.KNOWN_RECORD_TYPES:
                stats["content_types"][t] = stats["content_types"].get(t, 0) + 1
            if isinstance(_c, list):
                for _b in _c:
                    if isinstance(_b, dict):
                        bt = _b.get("type")
                        stats["seen_blocks"][bt] = stats["seen_blocks"].get(bt, 0) + 1
            # Records that are NOT conversation, even though Claude Code files them as
            # `user`. A /compact summary in particular is Claude's digest of the whole
            # session INCLUDING tool results — indexing it smuggles tool output (mail bodies,
            # query results, file contents) into an archive whose premise is that it holds
            # none. Before this guard a real archive had dozens of such turns, with addresses.
            if d.get("isCompactSummary") or d.get("isVisibleInTranscriptOnly") \
                    or d.get("isMeta"):
                stats["skipped_meta"] += 1
                continue
            sid = d.get("sessionId") or d.get("session_id")
            if sid:
                sid_seen = sid
            sid = sid or sid_seen
            if not sid:
                continue
            if t not in ("user", "assistant", "custom-title", "ai-title", "agent-name"):
                continue
            m = meta.setdefault(
                sid,
                {"cwd": "", "branch": "", "title": "", "title_custom": False,
                 "first": "", "start": "", "end": "", "entry": ""},
            )

            if t == "custom-title" and d.get("customTitle"):
                m["title"], m["title_custom"] = d["customTitle"], True
                continue
            if t == "ai-title" and d.get("aiTitle") and not m["title"]:
                m["title"] = d["aiTitle"]
                continue
            if t == "agent-name" and d.get("agentName") and not m["title"]:
                m["title"] = d["agentName"]
                continue
            if t not in ("user", "assistant"):
                continue

            if d.get("entrypoint"):
                m["entry"] = d["entrypoint"]
            if d.get("cwd"):
                m["cwd"] = d["cwd"]
            if d.get("gitBranch"):
                m["branch"] = d["gitBranch"]
            ts = d.get("timestamp") or ""
            if ts:
                if not m["start"] or ts < m["start"]:
                    m["start"] = ts
                if ts > m["end"]:
                    m["end"] = ts

            side = 1 if d.get("isSidechain") else 0
            content = (d.get("message") or {}).get("content")
            text = L.clean_text(L.blocks_text(content))
            # `claude -p` / SDK sessions (entrypoint "sdk-cli"): the "user" text is a
            # PROGRAM's payload (documents, transcripts, data dumps), not something a person
            # typed. Keep the replies and touches, drop the input.
            if t == "user" and d.get("entrypoint") == "sdk-cli":
                text = ""
            if len(text) >= 2:
                seq += 1
                cur = conn.execute(
                    "INSERT INTO turns(session_id,file_id,seq,role,is_sidechain,ts,"
                    "line_no,text) VALUES(?,?,?,?,?,?,?,?)",
                    (sid, fid, seq, t, side, ts, line_no, text),
                )
                conn.execute(
                    "INSERT INTO turns_fts(rowid,text) VALUES(?,?)", (cur.lastrowid, text)
                )
                stats["turns"] += 1
                stats["text_bytes"] += len(text.encode())
                # `<command-name>/clear</command-name>` was showing up as four sessions'
                # displayed titles; anything starting with a tag is not a first message.
                if t == "user" and not side and not m["first"] \
                        and not text.startswith("<"):
                    m["first"] = text[:400]

            if t == "assistant":
                for name, inp in _tool_uses(content):
                    vals = [("tool", name)]
                    if isinstance(inp, dict):
                        for k in PATH_KEYS:
                            v = inp.get(k)
                            if isinstance(v, str) and v:
                                vals.append(("file", L.redact(v[:400])))
                        cmd = inp.get("command")
                        if isinstance(cmd, str) and cmd:
                            vals.append(("command", L.redact(cmd[:CMD_MAX])))
                    for kind, value in vals:
                        cur = conn.execute(
                            "INSERT OR IGNORE INTO touches"
                            "(session_id,file_id,kind,value,n,first_ts,last_ts)"
                            " VALUES(?,?,?,?,1,?,?)",
                            (sid, fid, kind, value, ts, ts),
                        )
                        if cur.rowcount:
                            conn.execute(
                                "INSERT INTO touches_fts(rowid,value) VALUES(?,?)",
                                (cur.lastrowid, value),
                            )
                            stats["touches"] += 1
                        else:
                            conn.execute(
                                "UPDATE touches SET n=n+1, last_ts=? WHERE session_id=?"
                                " AND file_id=? AND kind=? AND value=?",
                                (ts, sid, fid, kind, value),
                            )
                            stats["touch_dups"] += 1

    # Merge against what is already stored. An incremental pass often sees ONLY a title
    # record for a session (Claude Code appends `ai-title` after the first reply), and a
    # naive upsert then wrote cwd='' / project=<slug> / git_branch='' over good values —
    # and let that ai-title replace a custom-title the user had chosen.
    for sid, m in meta.items():
        prev = conn.execute(
            "SELECT cwd, project, git_branch, title, first_user_msg, started_at, ended_at,"
            " entrypoint FROM session_files WHERE session_id=? AND src_path=?", (sid, path)
        ).fetchone()
        pv = (lambda k: (prev[k] if prev and prev[k] else ""))
        cwd = m["cwd"] or pv("cwd")
        project = L.project_for(m["cwd"], path) if m["cwd"] else (pv("project") or
                                                                  L.project_for("", path))
        branch = m["branch"] or pv("git_branch")
        if m["title_custom"]:
            title = m["title"]                      # an explicit rename always wins
        else:
            title = pv("title") or m["title"]       # never downgrade custom -> ai
        title = L.redact(title)
        first = pv("first_user_msg") or m["first"]
        starts = [x for x in (m["start"], pv("started_at")) if x]
        start = min(starts) if starts else ""
        end = max(m["end"], pv("ended_at"))
        entry = m["entry"] or pv("entrypoint")
        conn.execute(
            "INSERT INTO session_files(session_id,src_path,cwd,project,git_branch,title,"
            "first_user_msg,started_at,ended_at,entrypoint) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id,src_path) DO UPDATE SET cwd=excluded.cwd,"
            " project=excluded.project, git_branch=excluded.git_branch,"
            " title=excluded.title, first_user_msg=excluded.first_user_msg,"
            " started_at=excluded.started_at, ended_at=excluded.ended_at,"
            " entrypoint=excluded.entrypoint",
            (sid, path, cwd, project, branch, title, first, start, end, entry),
        )

    conn.execute(
        "INSERT INTO files_seen(path,size,mtime,offset,line_no,last_scan)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size,"
        " mtime=excluded.mtime, offset=excluded.offset, line_no=excluded.line_no,"
        " last_scan=excluded.last_scan",
        (path, size, mtime, pos, line_no, _now()),
    )


def rebuild_sessions(conn) -> int:
    """Derive `sessions` from session_files + turns. Aggregates across resumed files."""
    now = _now()
    conn.execute("DELETE FROM sessions")
    conn.execute(
        """
        INSERT INTO sessions(session_id, jsonl_path, jsonl_exists, project, cwd,
                             git_branch, title, first_user_msg, started_at, ended_at,
                             n_user, n_assistant, bytes_text, indexed_at, entrypoint)
        SELECT f.session_id,
               (SELECT src_path FROM session_files x WHERE x.session_id=f.session_id
                 ORDER BY (x.src_path LIKE '%/subagents/%'), x.ended_at DESC LIMIT 1),
               0,
               (SELECT project FROM session_files x WHERE x.session_id=f.session_id
                 AND x.project<>'' ORDER BY x.ended_at DESC LIMIT 1),
               (SELECT cwd FROM session_files x WHERE x.session_id=f.session_id
                 AND x.cwd<>'' ORDER BY x.ended_at DESC LIMIT 1),
               (SELECT git_branch FROM session_files x WHERE x.session_id=f.session_id
                 AND x.git_branch<>'' ORDER BY x.ended_at DESC LIMIT 1),
               (SELECT title FROM session_files x WHERE x.session_id=f.session_id
                 AND x.title<>'' ORDER BY x.ended_at DESC LIMIT 1),
               (SELECT first_user_msg FROM session_files x WHERE x.session_id=f.session_id
                 AND x.first_user_msg<>'' ORDER BY x.started_at LIMIT 1),
               MIN(NULLIF(f.started_at,'')), MAX(f.ended_at),
               (SELECT COUNT(*) FROM turns t WHERE t.session_id=f.session_id AND t.role='user'),
               (SELECT COUNT(*) FROM turns t WHERE t.session_id=f.session_id AND t.role='assistant'),
               (SELECT COALESCE(SUM(LENGTH(t.text)),0) FROM turns t WHERE t.session_id=f.session_id),
               ?,
               (SELECT entrypoint FROM session_files x WHERE x.session_id=f.session_id
                 AND x.entrypoint<>'' ORDER BY x.ended_at DESC LIMIT 1)
          FROM session_files f GROUP BY f.session_id
        """,
        (now,),
    )
    conn.execute(
        "UPDATE sessions SET jsonl_exists = (jsonl_path IS NOT NULL AND jsonl_path<>'')"
    )
    for r in conn.execute("SELECT session_id, jsonl_path FROM sessions").fetchall():
        conn.execute(
            "UPDATE sessions SET jsonl_exists=? WHERE session_id=?",
            (1 if r["jsonl_path"] and os.path.exists(r["jsonl_path"]) else 0,
             r["session_id"]),
        )
    return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def sync_memories(conn, stats) -> None:
    """Mirror every project's memory/*.md into `memories`. Adds, updates AND deletes.

    Not incremental by offset like transcripts: memory files are rewritten in place, so
    the unit of change is the whole file, keyed on (size, mtime).
    """
    now = _now()
    on_disk = {}
    for p in L.memory_files():
        try:
            st = os.stat(p)
        except OSError:
            continue
        on_disk[p] = (st.st_size, st.st_mtime)
    have = {r["path"]: (r["mem_id"], r["size"], r["mtime"])
            for r in conn.execute("SELECT mem_id, path, size, mtime FROM memories")}

    def _drop(mem_id):
        conn.execute("DELETE FROM memories_fts WHERE rowid=?", (mem_id,))
        conn.execute("DELETE FROM memories WHERE mem_id=?", (mem_id,))

    for path, (mem_id, _, _) in have.items():
        if path not in on_disk:
            _drop(mem_id)
            stats["mem_removed"] += 1

    for path, (size, mtime) in on_disk.items():
        old = have.get(path)
        if old and old[1] == size and old[2] == mtime:
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
        except OSError:
            continue
        m = L.parse_memory(raw, Path(path).stem)
        name, desc = L.redact(m["name"]), L.redact(m["description"])
        body = L.clean_text(m["body"])
        if old:
            _drop(old[0])
        project = L.slug_to_project(Path(path).parent.parent.name)
        cur = conn.execute(
            "INSERT INTO memories(path, project, name, description, mtype, body,"
            " size, mtime, indexed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (path, project, name, desc, m["mtype"], body, size, mtime, now))
        conn.execute("INSERT INTO memories_fts(rowid, text) VALUES(?,?)",
                     (cur.lastrowid, f"{name}\n{desc}\n{body}"))
        stats["mem_updated" if old else "mem_added"] += 1


def embed_pending(conn, stats, cap: int, timeout: float) -> None:
    """Give vectors to turns and memories that lack one. Bounded, and never fatal.

    Only when RECALL_EMBED_URL is set. The server may be down or still loading the model:
    any failure just leaves the rest for the next run — search uses keywords meanwhile.
    Pipeline and subagent turns are not embedded; search hides them by default anyway.
    """
    if not L.EMBED_ENABLED:
        return
    todo = [("t", r["turn_id"], r["text"]) for r in conn.execute(
        "SELECT t.turn_id, t.text FROM turns t JOIN sessions s ON s.session_id=t.session_id"
        " WHERE t.is_sidechain=0 AND COALESCE(s.entrypoint,'')<>'sdk-cli'"
        " AND LENGTH(t.text) >= ? AND t.turn_id NOT IN (SELECT turn_id FROM turn_vecs)"
        " ORDER BY t.ts DESC", (L.EMBED_MIN_CHARS,))]
    todo = [("m", r["mem_id"], f"{r['name']}\n{r['description']}\n{r['body']}") for r in
            conn.execute("SELECT mem_id, name, description, body FROM memories"
                         " WHERE vec IS NULL")] + todo
    stats["embed_pending"] = len(todo)
    if cap > 0:
        todo = todo[:cap]
    done = 0
    try:
        for i in range(0, len(todo), 32):
            batch = todo[i:i + 32]
            vecs = L.embed([txt[:L.EMBED_HEAD] for _, _, txt in batch], timeout)
            for (kind, rid, _), v in zip(batch, vecs):
                if kind == "t":
                    conn.execute("INSERT OR REPLACE INTO turn_vecs(turn_id, vec) VALUES(?,?)",
                                 (rid, v.tobytes()))
                else:
                    conn.execute("UPDATE memories SET vec=? WHERE mem_id=?", (v.tobytes(), rid))
            conn.commit()
            done += len(batch)
    except Exception as exc:
        stats["embed_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    stats["embedded"] = done


def record_drift(conn, stats) -> None:
    """Merge the record/block types seen in this scan into meta; `verify` reads them."""
    for key in ("seen_types", "seen_blocks", "content_types"):
        row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        merged = json.loads(row["v"]) if row else {}
        for k, n in stats[key].items():
            merged[str(k)] = merged.get(str(k), 0) + n
        conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE"
                     " SET v=excluded.v", (key, json.dumps(merged)))
    new_t = sorted(str(k) for k in stats["seen_types"] if k not in L.KNOWN_RECORD_TYPES)
    new_b = sorted(str(k) for k in stats["seen_blocks"] if k not in L.KNOWN_BLOCK_TYPES)
    stats["drift"] = (new_t, new_b)


def main() -> int:
    ap = argparse.ArgumentParser(description="index Claude Code transcripts for recall")
    ap.add_argument("--full", action="store_true",
                    help="re-read every transcript and memory file from scratch; sessions "
                         "whose .jsonl is already gone are kept, never wiped")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--db", default=None, help="override index path (tests)")
    ap.add_argument("--embed-cap", type=int, default=500,
                    help="max turns to embed this run (0 = skip, -1 = all)")
    ap.add_argument("--embed-timeout", type=float, default=60.0)
    args = ap.parse_args()
    L.utf8_stdio()
    # seed slug -> project names from cwds already in the index, so memory folders and
    # scratchpads of projects not re-scanned this run still get readable names
    t0 = time.time()
    # One writer at a time. Two extracts (a search from each of two sessions, or a search
    # during a scheduled run) both read the same files_seen.offset and both inserted the
    # appended lines — duplicate turns, reproduced in review. Whoever holds the lock is doing
    # this exact work, so the loser just leaves.
    lock = L.acquire_lock(Path(args.db) if args.db else L.DB_PATH, blocking=False)
    if lock is None:
        if not args.quiet:
            print("extract already running — skipped")
        return 0
    conn = L.connect(Path(args.db) if args.db else None)
    L.ensure_schema(conn)
    for r in conn.execute("SELECT DISTINCT cwd, project FROM session_files"
                          " WHERE cwd<>'' AND project<>''"):
        L.KNOWN_SLUGS.setdefault(L._slug(r["cwd"]), r["project"])
    if args.full:
        # Do NOT wipe turns/touches/session_files/sessions here. scan_file(full=True)
        # already drops each on-disk file's rows before re-reading it, so a table wipe
        # adds exactly one effect: it destroys every session whose .jsonl Claude Code has
        # already deleted (cleanupPeriodDays) — text that exists nowhere else (this happened
        # once and had to be restored from a backup). Archived rows keep whatever extraction
        # rules were in force when they were indexed — that is the price of keeping them.
        for t in ("files_seen", "memories_fts", "memories"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()

    files = L.transcript_files()
    # `forget` records the source files it erased. Without this the very next search
    # (which refreshes the index) re-scanned them from offset 0 and brought the session
    # straight back — forgetting lasted until the next query.
    forgotten = {r["k"][len("forgotten:"):]
                 for r in conn.execute("SELECT k FROM meta WHERE k LIKE 'forgotten:%'")}
    for gone in [p for p in forgotten if not os.path.exists(p)]:
        conn.execute("DELETE FROM meta WHERE k=?", ("forgotten:" + gone,))
        forgotten.discard(gone)
    if forgotten:
        files = [f for f in files if f not in forgotten]
        stats_skipped_forgotten = len(forgotten)
    else:
        stats_skipped_forgotten = 0
    stats = {"turns": 0, "touches": 0, "touch_dups": 0, "text_bytes": 0,
             "mem_added": 0, "mem_updated": 0, "mem_removed": 0,
             "seen_types": {}, "seen_blocks": {}, "content_types": {},
             "skipped_meta": 0,
             "bad_lines": 0, "partial_tail": 0, "rescanned": 0,
             "files": len(files), "errors": 0}
    for i, path in enumerate(files, 1):
        try:
            scan_file(conn, path, args.full, stats)
        except Exception as exc:  # one bad file must not kill the nightly run
            stats["errors"] += 1
            print(f"  !! {path}: {exc}", file=sys.stderr)
        if i % 50 == 0:
            conn.commit()
    conn.commit()

    try:
        sync_memories(conn, stats)
    except Exception as exc:
        stats["errors"] += 1
        print(f"  !! memories: {exc}", file=sys.stderr)
    conn.commit()

    n_sessions = rebuild_sessions(conn)
    record_drift(conn, stats)
    if args.embed_cap != 0:
        embed_pending(conn, stats, args.embed_cap, args.embed_timeout)
    conn.execute(
        "INSERT INTO meta(k,v) VALUES('last_scan',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (_now(),))
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    if args.full:
        conn.execute("VACUUM")   # a full rebuild leaves a lot of free pages behind
    conn.close()

    dt = time.time() - t0
    if args.quiet:
        # One line even when quiet: this is what lands in a scheduler's log, and without it
        # "did last night's run happen?" is unanswerable.
        print(f"{_now()} ok {n_sessions} sessions +{stats['turns']} turns "
              f"+{stats['touches']} touches in {dt:.1f}s"
              + (f" mem +{stats['mem_added']}/~{stats['mem_updated']}/-{stats['mem_removed']}"
                 if stats["mem_added"] or stats["mem_updated"] or stats["mem_removed"] else "")
              + (f" embed +{stats.get('embedded', 0)}/{stats.get('embed_pending', 0)}"
                 if stats.get("embed_pending") else "")
              + (f" [embed skipped: {stats['embed_error']}]" if stats.get("embed_error") else "")
              + (f" [bad_lines={stats['bad_lines']}]" if stats["bad_lines"] else "")
              + (f" [DRIFT types={stats['drift'][0]} blocks={stats['drift'][1]}]"
                 if any(stats.get("drift", ([], []))) else "")
              + (f" [errors={stats['errors']}]" if stats["errors"] else ""), flush=True)
    if not args.quiet:
        print(f"scanned {stats['files']} files in {dt:.1f}s -> {n_sessions} sessions, "
              f"+{stats['turns']} turns, +{stats['touches']} touches, "
              f"{stats['text_bytes']/1e6:.1f} MB text")
        if stats_skipped_forgotten:
            print(f"  forgotten files skipped: {stats_skipped_forgotten}")
        if any(stats.get("drift", ([], []))):
            print(f"  ⚠️ format drift — record types {stats['drift'][0]} · blocks {stats['drift'][1]}"
                  " · run recall.py verify")
        if stats.get("embed_pending"):
            print(f"  embedded {stats.get('embedded', 0)} of {stats['embed_pending']} pending"
                  + (f" — stopped: {stats['embed_error']}" if stats.get("embed_error") else ""))
        for k in ("mem_added", "mem_updated", "mem_removed",
                  "rescanned", "bad_lines", "partial_tail", "skipped_meta", "errors"):
            if stats[k]:
                print(f"  {k}: {stats[k]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
