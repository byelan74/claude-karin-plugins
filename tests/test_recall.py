#!/usr/bin/env python3
"""Test suite for claude-recall.   python3 tests/test_recall.py

Everything here runs on synthetic fixtures: a throwaway archive in a temp dir
(RECALL_PROJECTS_DIR / RECALL_DB / RECALL_HOME), driven through extract.py / recall.py as
subprocesses — real end-to-end, no mocks. Your own ~/.claude is never read or written.

The secret test is the important one, and it is arranged so it cannot be fudged: the
ORACLE is recall_lib.LEAK_PATTERNS. If a redactor is narrower than a detector the fixture
leaks and this suite fails. Fix the redactor — never the detector.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fixtures and subprocess output carry Thai text and emoji. Without UTF-8 mode, Python on a
# Thai-locale Windows reads and writes them as cp874: fixture lines become invalid JSON and
# subprocess output fails to decode (measured on Windows 11: 10 FAIL + a crash). Re-run in
# UTF-8 mode; PYTHONUTF8 carries it into every child process too. subprocess, not execv —
# on Windows execv returns to the caller before the child finishes and loses the exit code.
if not sys.flags.utf8_mode:
    os.environ["PYTHONUTF8"] = "1"
    raise SystemExit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))

ROOT = Path(__file__).resolve().parent.parent
HERE = ROOT / "plugins" / "recall" / "scripts"
sys.path.insert(0, str(HERE))

# Isolation BEFORE recall_lib is imported (it reads the environment at import time) — and
# every subprocess inherits the same values:
#   * a fake multi-project workspace, so the RECALL_WORKSPACE grouping is exercised
#   * a throwaway RECALL_HOME, so nothing is written next to the user's real index
#   * a fixed timezone, so the local-date assertions mean the same thing on every machine.
#     POSIX form "ICT-7" (= UTC+7), not "Asia/Bangkok": the Windows CRT misparses IANA names
#     as UTC, while glibc, macOS and Windows all read the POSIX form the same way.
#   * a dead embedding endpoint: fixture text never reaches a real embedding server;
#     semantic_tests() swaps in its own fake server
_SANDBOX = Path(tempfile.mkdtemp(prefix="recall-suite-"))
_WS_DIR = _SANDBOX / "Workspace"
for _d in ("alpha_app", "beta_tool", "gamma_svc"):
    (_WS_DIR / _d).mkdir(parents=True)
os.environ["RECALL_WORKSPACE"] = str(_WS_DIR)
os.environ["RECALL_HOME"] = str(_SANDBOX / "home")
os.environ["TZ"] = "ICT-7"
if hasattr(time, "tzset"):
    time.tzset()
os.environ["RECALL_EMBED_URL"] = "http://127.0.0.1:9/v1/embeddings"
for _k in ("RECALL_EXTRA_PROJECTS", "CLAUDE_CONFIG_DIR"):
    os.environ.pop(_k, None)

import recall_lib as L  # noqa: E402

PY = sys.executable
EXTRACT = str(HERE / "extract.py")
RECALL = str(HERE / "recall.py")

_results: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return bool(ok)


# ---------------------------------------------------------------- fixture

WS = str(L.WORKSPACE)          # the fake workspace above (resolved)
SECRETS = [
    "sk-ant-api03-ABCdef123",          # detector: sk-[A-Za-z0-9]
    "api_key=SUPERSECRETVALUE",        # detector: api[_-]?key *[=:]
    "API-KEY: another-secret",
    "Authorization: Bearer eyJhbGciOi.JIUzI1",
    "password = hunter2",
    "pk_live_abc",                     # short on purpose: the {8,} trap
    "stripe_live_ABCDEFGH12345",
]


def _rec(**kw):
    # every real user/assistant record carries entrypoint (657/657 files, 2026-09-23);
    # verify now fails on recent sessions without one, so fixtures must look real
    if kw.get("type") in ("user", "assistant"):
        kw.setdefault("entrypoint", "cli")
    return json.dumps(kw, ensure_ascii=False) + "\n"


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
        timespec="seconds")


def build_fixture(root: Path) -> dict:
    """Two sessions in two project dirs, one of them a scratchpad path."""
    slug_a = L._slug(WS + "/outlook_mailer")
    slug_b = L._slug(WS + "/alpha_app")
    (root / slug_a).mkdir(parents=True)
    (root / slug_b).mkdir(parents=True)

    sid_a = "aaaaaaaa-1111-2222-3333-444444444444"
    sid_b = "bbbbbbbb-1111-2222-3333-444444444444"
    fa = root / slug_a / f"{sid_a}.jsonl"
    fb = root / slug_b / f"{sid_b}.jsonl"

    lines_a = [
        _rec(type="ai-title", sessionId=sid_a, aiTitle="Fixture Alpha"),
        _rec(type="user", sessionId=sid_a, timestamp=_ts(3),
             cwd=WS + "/outlook_mailer",
             message={"content": "ช่วยดู ใบเสนอราคา ของ vendor หน่อย unique-alpha-token"}),
        _rec(type="assistant", sessionId=sid_a, timestamp=_ts(3),
             cwd=WS + "/outlook_mailer",
             message={"content": [
                 {"type": "thinking", "thinking": "THINKING-MUST-NOT-BE-INDEXED"},
                 {"type": "text", "text": "ดูให้แล้วครับ " + SECRETS[0] + " และ " + SECRETS[5]},
                 {"type": "tool_use", "name": "Bash",
                  "input": {"command": "curl -H 'Authorization: Bearer LEAKYTOKEN' x"}},
                 {"type": "tool_use", "name": "Read",
                  "input": {"file_path": WS + "/outlook_mailer/mailer.py"}},
             ]}),
        _rec(type="user", sessionId=sid_a, timestamp=_ts(3),
             message={"content": [
                 {"type": "tool_result", "tool_use_id": "x",
                  "content": "TOOLRESULT-MUST-NOT-BE-INDEXED " + SECRETS[1]}]}),
        _rec(type="user", sessionId=sid_a, timestamp=_ts(3),
             message={"content": "<system-reminder>REMINDER-MUST-NOT-BE-INDEXED</system-reminder>"
                                 " ต่อเลย"}),
        _rec(type="assistant", sessionId=sid_a, timestamp=_ts(3), isSidechain=True,
             message={"content": [{"type": "text",
                                   "text": "SIDECHAIN-TEXT unique-sidechain-token"}]}),
    ]
    # session B lives in a scratchpad cwd -> must be reported as project AIOS
    scratch = f"/private/tmp/claude-501/{slug_b}/deadbeef/scratchpad"
    lines_b = [
        _rec(type="custom-title", sessionId=sid_b, customTitle="Fixture Bravo"),
        _rec(type="user", sessionId=sid_b, timestamp=_ts(30), cwd=scratch,
             message={"content": "ทดสอบ alpha แบบ unique-bravo-token กับ " + SECRETS[2]}),
        _rec(type="assistant", sessionId=sid_b, timestamp=_ts(30), cwd=scratch,
             message={"content": [{"type": "text",
                                   "text": SECRETS[3] + " / " + SECRETS[4] + " / " + SECRETS[6]}]}),
    ]
    fa.write_text("".join(lines_a))
    fb.write_text("".join(lines_b))
    return {"sid_a": sid_a, "sid_b": sid_b, "fa": fa, "fb": fb}


def run(args, env, **kw):
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=300, **kw)


def fixture_tests() -> None:
    print("\n=== FIXTURE (throwaway archive, real index untouched) ===")
    tmp = Path(tempfile.mkdtemp(prefix="recall-test-"))
    try:
        proj = tmp / "projects"
        db = tmp / "test.db"
        env = dict(os.environ, RECALL_PROJECTS_DIR=str(proj), RECALL_DB=str(db))
        fx = build_fixture(proj)

        r = run([PY, EXTRACT, "--full"], env)
        check("extract runs on fixture", r.returncode == 0, r.stderr.strip()[:120])

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        texts = [r["text"] for r in conn.execute("SELECT text FROM turns")]
        blob = "\n".join(texts)

        # --- the oracle test
        leaks = sorted({p for t in texts for p in L.detector_hits(t)})
        check("no secret survives into the index", not leaks, f"leaked: {leaks}")
        check("redaction actually fired", "[REDACTED]" in blob)

        check("thinking blocks excluded", "THINKING-MUST-NOT-BE-INDEXED" not in blob)
        check("tool_result excluded", "TOOLRESULT-MUST-NOT-BE-INDEXED" not in blob)
        check("system-reminder stripped", "REMINDER-MUST-NOT-BE-INDEXED" not in blob)

        cmds = [r["value"] for r in conn.execute(
            "SELECT value FROM touches WHERE kind='command'")]
        check("command redacted in touches",
              cmds and all(not L.detector_hits(c) for c in cmds), str(cmds)[:90])
        files = [r["value"] for r in conn.execute(
            "SELECT value FROM touches WHERE kind='file'")]
        check("file paths captured", any("mailer.py" in f for f in files), str(files)[:90])

        s_a = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                           (fx["sid_a"],)).fetchone()
        s_b = conn.execute("SELECT * FROM sessions WHERE session_id=?",
                           (fx["sid_b"],)).fetchone()
        check("ai-title captured", s_a["title"] == "Fixture Alpha", s_a["title"])
        check("custom-title captured", s_b["title"] == "Fixture Bravo", s_b["title"])
        check("project from cwd", s_a["project"] == "outlook_mailer", s_a["project"])
        check("scratchpad maps to parent project", s_b["project"] == "alpha_app", s_b["project"])
        check("first_user_msg set", "ใบเสนอราคา" in (s_a["first_user_msg"] or ""))
        side = conn.execute("SELECT COUNT(*) FROM turns WHERE is_sidechain=1").fetchone()[0]
        check("sidechain turn stored but flagged", side == 1, f"n={side}")
        conn.close()

        # --- incremental
        n1 = _count(db, "turns")
        r = run([PY, EXTRACT, "--quiet"], env)
        n2 = _count(db, "turns")
        check("re-run is idempotent (no duplicates)", n1 == n2, f"{n1} -> {n2}")

        with open(fx["fa"], "a") as fh:
            fh.write(_rec(type="user", sessionId=fx["sid_a"], timestamp=_ts(0),
                          cwd=WS + "/outlook_mailer",
                          message={"content": "appended-line-token ทดสอบต่อท้าย"}))
        run([PY, EXTRACT, "--quiet"], env)
        n3 = _count(db, "turns")
        check("incremental picks up an appended turn", n3 == n2 + 1, f"{n2} -> {n3}")

        # --- half-written final line must not be consumed
        with open(fx["fa"], "a") as fh:
            fh.write('{"type":"user","sessionId":"' + fx["sid_a"] + '","message":{"conte')
        run([PY, EXTRACT, "--quiet"], env)
        n4 = _count(db, "turns")
        check("partial tail line is not indexed", n4 == n3, f"{n3} -> {n4}")
        # completing the line must then index it, i.e. the offset was not advanced
        with open(fx["fa"], "a") as fh:
            fh.write('nt":"completed-after-partial"},"timestamp":"' + _ts(0) + '"}\n')
        run([PY, EXTRACT, "--quiet"], env)
        n5 = _count(db, "turns")
        check("completed line is picked up afterwards", n5 == n4 + 1, f"{n4} -> {n5}")
        check("no offset corruption",
              _scalar(db, "SELECT COUNT(*) FROM turns WHERE text LIKE '%completed-after-partial%'") == 1)

        # --- rewritten (shrunk) file is re-scanned, not duplicated
        fx["fb"].write_text(_rec(type="user", sessionId=fx["sid_b"], timestamp=_ts(1),
                                 cwd=WS + "/alpha_app",
                                 message={"content": "rewritten-bravo-token สั้นลง"}))
        run([PY, EXTRACT, "--quiet"], env)
        nb = _scalar(db, "SELECT COUNT(*) FROM turns WHERE session_id='%s'" % fx["sid_b"])
        check("shrunk file triggers a clean re-scan", nb == 1, f"turns for B = {nb}")
        check("old rows of rewritten file are gone",
              _scalar(db, "SELECT COUNT(*) FROM turns WHERE text LIKE '%unique-bravo-token%'") == 0)

        # --- search
        out = run([PY, RECALL, "--no-refresh", "search", "unique-alpha-token", "--json"], env)
        j = json.loads(out.stdout or "{}")
        check("search finds an english token", bool(j.get("results")), out.stdout[:80])
        out = run([PY, RECALL, "--no-refresh", "search", "ใบเสนอราคา", "--json"], env)
        j = json.loads(out.stdout or "{}")
        check("search finds a Thai phrase (trigram)", bool(j.get("results")))
        out = run([PY, RECALL, "--no-refresh", "search", "unique-sidechain-token", "--json"], env)
        check("sidechain hidden by default", not json.loads(out.stdout or "{}").get("results"))
        out = run([PY, RECALL, "--no-refresh", "search", "unique-sidechain-token",
                   "--sidechain", "--json"], env)
        check("sidechain findable with --sidechain",
              bool(json.loads(out.stdout or "{}").get("results")))
        out = run([PY, RECALL, "--no-refresh", "search", "ดู", "--json"], env)
        check("short (<3 char) query does not crash", out.returncode in (0, 1),
              out.stderr.strip()[:80])

        r = run([PY, RECALL, "--no-refresh", "verify", "--sample", "5"], env)
        check("verify passes on the fixture", r.returncode == 0, r.stdout.strip()[-90:])
        check("verify's round-trip actually sampled something",
              "0/0" not in r.stdout,
              [l for l in r.stdout.splitlines() if "round-trip" in l][:1])

        # --- forget
        run([PY, RECALL, "--no-refresh", "forget", fx["sid_a"]], env)
        check("forget removes the session",
              _scalar(db, "SELECT COUNT(*) FROM sessions WHERE session_id='%s'" % fx["sid_a"]) == 0)
        check("forget removes its turns",
              _scalar(db, "SELECT COUNT(*) FROM turns WHERE session_id='%s'" % fx["sid_a"]) == 0)
        check("forget keeps fts in step",
              _scalar(db, "SELECT COUNT(*) FROM turns_fts") == _scalar(db, "SELECT COUNT(*) FROM turns"))
        check("forget does not delete the transcript", fx["fa"].exists())

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def memory_fixture_tests() -> None:
    """L1 memory files become searchable across projects (added 2026-09-23)."""
    print("\n=== MEMORY FILES (throwaway archive) ===")
    tmp = Path(tempfile.mkdtemp(prefix="recall-mem-"))
    try:
        proj = tmp / "projects"
        db = tmp / "test.db"
        env = dict(os.environ, RECALL_PROJECTS_DIR=str(proj), RECALL_DB=str(db))
        build_fixture(proj)
        mdir = proj / L._slug(WS + "/outlook_mailer") / "memory"
        mdir.mkdir()
        lesson = mdir / "gotcha-lookup.md"
        lesson.write_text(
            "---\nname: gotcha-lookup\ndescription: \"lookup-desc-token คืน 0 แถวเงียบ ๆ\"\n"
            "metadata:\n  type: reference\n---\n\nใช้ comp count แทน mem-body-token "
            + SECRETS[1] + "\n")
        (mdir / "MEMORY.md").write_text("- [x](gotcha-lookup.md) index-must-not-be-indexed\n")
        (mdir / "session_resume_current.md").write_text("resume-must-not-be-indexed\n")

        r = run([PY, EXTRACT, "--full"], env)
        check("extract indexes memory files", r.returncode == 0, r.stderr.strip()[:120])
        check("one memory row (MEMORY.md + session_resume skipped)",
              _count(db, "memories") == 1, f"n={_count(db, 'memories')}")
        check("memories_fts in step", _count(db, "memories_fts") == 1)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        m = conn.execute("SELECT * FROM memories").fetchone()
        conn.close()
        check("frontmatter parsed (name/description/nested type)",
              m["name"] == "gotcha-lookup" and "lookup-desc-token" in m["description"]
              and m["mtype"] == "reference", f"{m['name']} / {m['mtype']}")
        check("memory project from slug", m["project"] == "outlook_mailer", m["project"])
        check("secret redacted in memory body", not L.detector_hits(m["body"]), m["body"][-60:])

        def mem_search(q, *extra):
            out = run([PY, RECALL, "--no-refresh", "search", q, "--json", *extra], env)
            return json.loads(out.stdout or "{}")
        j = mem_search("mem-body-token")
        check("search returns the memory file", len(j.get("memories", [])) == 1,
              str(j.get("memories"))[:90])
        check("memory-only hit is not reported as 'not found'", bool(j.get("memories")))
        check("Thai query finds memory (trigram)", bool(mem_search("แถวเงียบ").get("memories")))
        check("MEMORY.md not searchable",
              not mem_search("index-must-not-be-indexed").get("memories"))
        check("session_resume not searchable",
              not mem_search("resume-must-not-be-indexed").get("memories"))
        check("--project filters memories out",
              not mem_search("mem-body-token", "--project", "AIOS").get("memories"))
        check("--memories 0 disables memory search",
              not mem_search("mem-body-token", "--memories", "0").get("memories"))

        r = run([PY, RECALL, "--no-refresh", "verify", "--sample", "3"], env)
        check("verify passes with memories", r.returncode == 0, r.stdout.strip()[-90:])

        # edit in place -> updated, not duplicated
        time.sleep(0.02)
        lesson.write_text(lesson.read_text().replace("mem-body-token", "mem-edited-token"))
        os.utime(lesson, None)
        run([PY, EXTRACT, "--quiet"], env)
        check("edited memory re-indexed, not duplicated",
              _count(db, "memories") == 1 and bool(mem_search("mem-edited-token").get("memories"))
              and not mem_search("mem-body-token").get("memories"))

        # --full must keep a session whose .jsonl is already gone (2026-09-23 regression:
        # the old table wipe destroyed 10 archived sessions on the real index)
        fb = next((proj / L._slug(WS + "/alpha_app")).glob("*.jsonl"))
        fb.unlink()
        n_before = _count(db, "sessions")
        run([PY, EXTRACT, "--full"], env)
        check("--full keeps sessions whose .jsonl was deleted",
              _count(db, "sessions") == n_before
              and _scalar(db, "SELECT COUNT(*) FROM sessions WHERE jsonl_exists=0") == 1,
              f"{n_before} -> {_count(db, 'sessions')}")
        check("--full keeps their text searchable",
              _scalar(db, "SELECT COUNT(*) FROM turns WHERE text LIKE '%unique-bravo-token%'") == 1)
        check("--full does not duplicate on-disk sessions",
              _scalar(db, "SELECT COUNT(*) FROM turns WHERE text LIKE '%unique-alpha-token%'") == 1)
        check("--full rebuilds memories", _count(db, "memories") == 1)

        # delete on disk -> gone from index (this is what "remove" means)
        lesson.unlink()
        run([PY, EXTRACT, "--quiet"], env)
        check("deleted memory leaves the index",
              _count(db, "memories") == 0 and _count(db, "memories_fts") == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def review_fix_tests() -> None:
    """Regressions from a code review (#1 env secrets … #8 apostrophe)."""
    print("\n=== REVIEW FIXES ===")
    import importlib.util
    spec = importlib.util.spec_from_file_location("recall_cli", RECALL)
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)

    # #1 env assignment
    fake = "export TENANT_FAKE_KEY='AbCdEfGh1234567890zz'"
    red = L.redact(fake)
    check("env *_KEY= value redacted", "[REDACTED]" in red and not L.detector_hits(red), red)
    check("detector sees an unredacted env key", bool(L.detector_hits(fake)))
    for keep in ("MAX_TOKEN=4096", "CONFIG_KEY=/home/x/cfg.json", "API_KEY_ID=41",
                 'AUTH_TOKEN="$TOKEN_VAR"'):
        check(f"env redactor leaves {keep!r} alone", L.redact(keep) == keep, L.redact(keep))

    # #7 scratchpad slug
    slug = "-private-tmp-claude-501-" + L._slug(WS + "/alpha_app") + "-abc-scratchpad"
    check("scratchpad slug maps to its project", L.slug_to_project(slug) == "alpha_app",
          L.slug_to_project(slug))
    # #8 apostrophe
    check("apostrophe inside a word is not a quote",
          R._terms("Alice's laptop don't") == ["Alice's", "laptop", "don't"],
          str(R._terms("Alice's laptop don't")))
    check("quoted phrase still works", R._terms("'hidden bar' x") == ["hidden bar", "x"])
    # #5 extra roots: $CLAUDE_CONFIG_DIR/projects and RECALL_EXTRA_PROJECTS are indexed
    code = ("import sys; sys.path.insert(0,%r); import recall_lib as L;"
            "print('|'.join(str(p) for p in L.PROJECTS_DIRS))" % str(HERE))
    env_r = {k: v for k, v in os.environ.items() if k != "RECALL_PROJECTS_DIR"}
    env_r.update(CLAUDE_CONFIG_DIR="/tmp/second-cfg",
                 RECALL_EXTRA_PROJECTS=os.pathsep.join(["/tmp/extra-a", "/tmp/extra-b"]))
    roots = run([PY, "-c", code], env_r).stdout.strip().split("|")
    check("default root + CLAUDE_CONFIG_DIR + extra roots are all indexed",
          roots[0].endswith(os.path.join(".claude", "projects"))
          and str(Path("/tmp/second-cfg") / "projects") in roots
          and str(Path("/tmp/extra-a")) in roots and str(Path("/tmp/extra-b")) in roots,
          str(roots))
    # without RECALL_WORKSPACE a project is simply the folder the session ran in
    code = ("import sys; sys.path.insert(0,%r); import recall_lib as L;"
            "print(L.project_for('/home/u/code/my_app/sub', 'z'), L.WORKSPACE)" % str(HERE))
    env_n = {k: v for k, v in os.environ.items() if k != "RECALL_WORKSPACE"}
    out_n = run([PY, "-c", code], env_n).stdout.split()
    check("no workspace: project = cwd folder name", out_n == ["sub", "None"], str(out_n))

    # #4 real timeout — a hung scan must not hold dir_by_slug past ~5 s
    code = ("import sys,time; sys.path.insert(0,%r); import recall_lib as L;"
            "L._scan_dir_names=lambda: time.sleep(20) or {}; t=time.time(); L.dir_by_slug();"
            "print(round(time.time()-t,1))" % str(HERE))
    t0 = time.time()
    r = run([PY, "-c", code], dict(os.environ))
    check("dir_by_slug gives up after ~5 s", time.time() - t0 < 9, r.stdout.strip())

    tmp = Path(tempfile.mkdtemp(prefix="recall-rev-"))
    try:
        proj = tmp / "projects"
        db = tmp / "test.db"
        env = dict(os.environ, RECALL_PROJECTS_DIR=str(proj), RECALL_DB=str(db))
        fx = build_fixture(proj)
        slug = L._slug(WS + "/beta_tool")
        (proj / slug).mkdir()
        sid_p = "cccccccc-1111-2222-3333-444444444444"
        (proj / slug / f"{sid_p}.jsonl").write_text(
            _rec(type="user", sessionId=sid_p, timestamp=_ts(1), entrypoint="sdk-cli",
                 cwd=WS + "/beta_tool",
                 message={"content": "[System Instructions] PIPELINE-PAYLOAD-MUST-NOT-BE-INDEXED"}) +
            _rec(type="assistant", sessionId=sid_p, timestamp=_ts(1), entrypoint="sdk-cli",
                 cwd=WS + "/beta_tool",
                 message={"content": [{"type": "text", "text": "pipeline-reply-token"}]}))
        sid_e = "dddddddd-1111-2222-3333-444444444444"
        (proj / slug / f"{sid_e}.jsonl").write_text(
            _rec(type="custom-title", sessionId=sid_e, customTitle="Empty"))
        run([PY, EXTRACT, "--full"], env)

        # #2 pipeline sessions
        check("pipeline user payload not indexed",
              _scalar(db, "SELECT COUNT(*) FROM turns WHERE text LIKE '%PIPELINE-PAYLOAD%'") == 0)
        check("pipeline reply kept",
              _scalar(db, "SELECT COUNT(*) FROM turns WHERE text LIKE '%pipeline-reply-token%'") == 1)
        check("entrypoint recorded on the session",
              _scalar(db, "SELECT COUNT(*) FROM sessions WHERE entrypoint='sdk-cli'") == 1)
        out = run([PY, RECALL, "--no-refresh", "search", "pipeline-reply-token", "--json"], env)
        check("pipeline hidden from search by default",
              not json.loads(out.stdout or "{}").get("results"))
        out = run([PY, RECALL, "--no-refresh", "search", "pipeline-reply-token", "--headless",
                   "--json"], env)
        check("--headless shows pipeline sessions",
              bool(json.loads(out.stdout or "{}").get("results")))
        out = run([PY, RECALL, "--no-refresh", "sessions"], env)
        check("pipeline hidden from sessions list", "cccccccc" not in out.stdout)

        # #6 show on a session with no turns
        r = run([PY, RECALL, "--no-refresh", "show", "dddddddd"], env)
        check("show on an empty session does not crash", "Traceback" not in r.stderr,
              r.stderr.strip()[-80:])

        # #3 two extracts at once must not duplicate
        for i in range(40):
            with open(fx["fa"], "a") as fh:
                fh.write(_rec(type="user", sessionId=fx["sid_a"], timestamp=_ts(0),
                              cwd=WS + "/outlook_mailer",
                              message={"content": f"concurrent-line-{i} ทดสอบ"}))
        ps = [subprocess.Popen([PY, EXTRACT, "--quiet"], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) for _ in range(3)]
        for p_ in ps:
            p_.wait(timeout=120)
        run([PY, EXTRACT, "--quiet"], env)     # the losers skipped; this one finishes
        dups = _scalar(db, "SELECT COUNT(*) FROM (SELECT 1 FROM turns GROUP BY file_id,"
                           " line_no, role HAVING COUNT(*) > 1)")
        n_conc = _scalar(db, "SELECT COUNT(*) FROM turns WHERE text LIKE 'concurrent-line-%'")
        check("concurrent extracts do not duplicate turns", dups == 0 and n_conc == 40,
              f"dups={dups} n={n_conc}")
        r = run([PY, RECALL, "--no-refresh", "verify", "--sample", "3"], env)
        check("verify passes after concurrent extracts", r.returncode == 0,
              r.stdout.strip()[-90:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def drift_tests() -> None:
    """#7 — a Claude Code format change must make verify fail, not pass silently."""
    print("\n=== FORMAT DRIFT ===")
    tmp = Path(tempfile.mkdtemp(prefix="recall-drift-"))
    try:
        proj = tmp / "projects"
        db = tmp / "test.db"
        env = dict(os.environ, RECALL_PROJECTS_DIR=str(proj), RECALL_DB=str(db))
        fx = build_fixture(proj)
        run([PY, EXTRACT, "--full"], env)
        r = run([PY, RECALL, "--no-refresh", "verify", "--sample", "2"], env)
        check("clean fixture: no drift reported", r.returncode == 0 and "new record types -" in r.stdout,
              r.stdout.strip()[-80:])

        with open(fx["fa"], "a") as fh:
            fh.write(_rec(type="brand-new-kind", sessionId=fx["sid_a"], timestamp=_ts(0),
                          message={"content": "conversation in a record we do not know"}))
            fh.write(_rec(type="user", sessionId=fx["sid_a"], timestamp=_ts(0),
                          cwd=WS + "/outlook_mailer",
                          message={"content": [{"type": "mystery", "payload": "x"}]}))
        r0 = run([PY, EXTRACT], env)
        check("extract reports drift in its own output", "format drift" in r0.stdout,
              r0.stdout.strip()[-100:])
        r = run([PY, RECALL, "--no-refresh", "verify", "--sample", "2"], env)
        check("verify fails on an unknown block type", r.returncode == 1 and "mystery" in r.stdout)
        check("verify names the unknown record type that carries content",
              "brand-new-kind" in r.stdout)

        # canary: a /compact summary that slipped past a renamed flag
        with open(fx["fa"], "a") as fh:
            fh.write(_rec(type="user", sessionId=fx["sid_a"], timestamp=_ts(0),
                          cwd=WS + "/outlook_mailer", isCompactSummaryV2=True,
                          message={"content": "This session is being continued from a previous"
                                              " conversation that ran out of context."}))
        run([PY, EXTRACT, "--quiet"], env)
        r = run([PY, RECALL, "--no-refresh", "verify", "--sample", "2"], env)
        check("canary catches a compact summary under a renamed flag",
              r.returncode == 1 and "compact summary" in r.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fake_embed_server():
    """Tiny stand-in for LM Studio /v1/embeddings: texts about signatures (either language)
    map to one direction, everything else to another. Returns (url, shutdown)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    def vec(text):
        t = text.lower()
        v = [0.0] * 1024
        v[0 if ("signature" in t or "ลายเซ็น" in t) else 1] = 1.0
        return v

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            out = {"data": [{"index": i, "embedding": vec(t)} for i, t in enumerate(body["input"])]}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/v1/embeddings", srv.shutdown


def semantic_tests() -> None:
    """#2 — semantic supplement: finds what keywords miss, never breaks keyword search."""
    print("\n=== SEMANTIC ===")
    import array
    u = array.array("f", [0.6, 0.8] + [0.0] * 1022)
    check("dot of unit vectors", abs(L.dot(u, u) - 1.0) < 1e-6)
    check("vector round-trips through bytes", list(L.unpack_vec(u.tobytes()))[:2] == list(u)[:2])

    tmp = Path(tempfile.mkdtemp(prefix="recall-sem-"))
    url, stop = _fake_embed_server()
    try:
        proj = tmp / "projects"
        db = tmp / "test.db"
        env = dict(os.environ, RECALL_PROJECTS_DIR=str(proj), RECALL_DB=str(db),
                   RECALL_EMBED_URL=url)
        fx = build_fixture(proj)
        with open(fx["fa"], "a") as fh:
            fh.write(_rec(type="assistant", sessionId=fx["sid_a"], timestamp=_ts(1),
                          cwd=WS + "/outlook_mailer",
                          message={"content": [{"type": "text", "text":
                              "แอปใส่ลายเซ็นให้เองอยู่แล้ว ถ้าเติมเองจะได้สองอัน sem-thai-token"}]}))
        r = run([PY, EXTRACT, "--full", "--embed-cap", "-1"], env)
        n_v = _count(db, "turn_vecs")
        check("extract embeds eligible turns", n_v >= 2, f"vectors={n_v}")
        check("short turns are not embedded",
              _scalar(db, "SELECT COUNT(*) FROM turn_vecs v JOIN turns t USING(turn_id)"
                          " WHERE LENGTH(t.text) < 40") == 0)

        out = run([PY, RECALL, "--no-refresh", "search", "signature", "--json"], env)
        j = json.loads(out.stdout or "{}")
        check("keyword alone misses the Thai turn", not j.get("results"), str(j.get("results"))[:60])
        check("semantic supplement finds it across languages",
              any("sem-thai-token" in x["snippet"] for x in j.get("semantic", [])),
              str(j.get("semantic"))[:90])
        out = run([PY, RECALL, "--no-refresh", "search", "signature", "--no-semantic", "--json"], env)
        check("--no-semantic turns it off", not json.loads(out.stdout or "{}").get("semantic"))

        out = run([PY, RECALL, "--no-refresh", "search", "unique-alpha-token", "--json"], env)
        j = json.loads(out.stdout or "{}")
        kw = {x["session_id"] for x in j.get("results", [])}
        check("semantic never repeats a keyword-hit session",
              not kw & {x["session_id"] for x in j.get("semantic", [])})

        dead = dict(env, RECALL_EMBED_URL="http://127.0.0.1:9/v1/embeddings")
        out = run([PY, RECALL, "--no-refresh", "search", "unique-alpha-token", "--json"], dead)
        j = json.loads(out.stdout or "{}")
        check("LM Studio down: keyword results still returned", bool(j.get("results")))
        check("LM Studio down: says so", bool(j.get("semantic_note")))
        r = run([PY, EXTRACT, "--quiet"], dead)
        check("LM Studio down: extract still succeeds", r.returncode == 0, r.stdout.strip()[-80:])

        run([PY, RECALL, "--no-refresh", "forget", fx["sid_a"]], env)
        check("forget removes the session's vectors",
              _scalar(db, "SELECT COUNT(*) FROM turn_vecs WHERE turn_id NOT IN"
                          " (SELECT turn_id FROM turns)") == 0)
        r = run([PY, RECALL, "--no-refresh", "verify", "--sample", "2"], env)
        check("verify passes with vectors", r.returncode == 0, r.stdout.strip()[-80:])
    finally:
        stop()
        shutil.rmtree(tmp, ignore_errors=True)


def qa_fixture_tests() -> None:
    """Regressions for everything the Fable QA pass found on 2026-09-21.

    Each check here corresponds to a bug that was live in the first build; several of them
    silently broke a promise the README made rather than raising anything.
    """
    print("\n=== QA REGRESSIONS ===")
    tmp = Path(tempfile.mkdtemp(prefix="recall-qa-"))
    try:
        proj = tmp / "projects"
        db = tmp / "qa.db"
        env = dict(os.environ, RECALL_PROJECTS_DIR=str(proj), RECALL_DB=str(db))
        slug = L._slug(WS + "/outlook_mailer")
        (proj / slug).mkdir(parents=True)
        sid = "dddddddd-1111-2222-3333-444444444444"
        f1 = proj / slug / f"{sid}.jsonl"

        f1.write_text("".join([
            _rec(type="custom-title", sessionId=sid, customTitle="Chosen By User"),
            _rec(type="user", sessionId=sid, timestamp=_ts(2), cwd=WS + "/outlook_mailer",
                 gitBranch="main",
                 message={"content": "ข้อความจริงยาวพอที่ round-trip check จะหยิบไปตรวจได้ qa-real-token"}),
            # a /compact summary: Claude-authored, contains tool-derived content
            _rec(type="user", sessionId=sid, timestamp=_ts(2), isCompactSummary=True,
                 isVisibleInTranscriptOnly=True,
                 message={"content": "This session is being continued... COMPACT-LEAK "
                                     "someone@example.com"}),
            _rec(type="user", sessionId=sid, timestamp=_ts(2), isMeta=True,
                 message={"content": "META-MUST-NOT-BE-INDEXED Base directory for this skill"}),
            _rec(type="user", sessionId=sid, timestamp=_ts(2),
                 message={"content": "<command-name>/clear</command-name> TAGSTART-TOKEN"}),
        ]))
        run([PY, EXTRACT, "--full"], env)
        blob = "\n".join(r[0] for r in sqlite3.connect(db).execute("SELECT text FROM turns"))
        check("compact summary is NOT indexed", "COMPACT-LEAK" not in blob)
        check("isMeta record is NOT indexed", "META-MUST-NOT-BE-INDEXED" not in blob)
        row = sqlite3.connect(db).execute(
            "SELECT title, first_user_msg, cwd, project, git_branch FROM sessions").fetchone()
        check("first_user_msg never starts with a tag", not (row[1] or "").startswith("<"),
              (row[1] or "")[:40])

        # --- metadata-only append must not blank cwd/project or downgrade the title
        with open(f1, "a") as fh:
            fh.write(_rec(type="ai-title", sessionId=sid, aiTitle="Auto Generated"))
        run([PY, EXTRACT, "--quiet"], env)
        row2 = sqlite3.connect(db).execute(
            "SELECT title, cwd, project, git_branch FROM sessions").fetchone()
        check("custom title survives an ai-title append", row2[0] == "Chosen By User", row2[0])
        check("cwd survives a metadata-only append", row2[1] == row[2], row2[1])
        check("project survives a metadata-only append", row2[2] == "outlook_mailer", row2[2])
        check("git branch survives a metadata-only append", row2[3] == "main", row2[3])

        # --- line_no must stay true after a complete-but-unterminated final line
        with open(f1, "a") as fh:
            fh.write(_rec(type="user", sessionId=sid, timestamp=_ts(1),
                          message={"content": "unterminated-line-token ทดสอบ"}).rstrip("\n"))
        run([PY, EXTRACT, "--quiet"], env)
        with open(f1, "a") as fh:
            fh.write("\n")
        r = run([PY, EXTRACT, "--quiet"], env)
        c = sqlite3.connect(db)
        ln = c.execute("SELECT line_no FROM turns WHERE text LIKE '%unterminated-line-token%'"
                       ).fetchone()
        nlines = len(f1.read_text().splitlines())
        check("turn from the unterminated line is indexed once",
              c.execute("SELECT COUNT(*) FROM turns WHERE text LIKE"
                        " '%unterminated-line-token%'").fetchone()[0] == 1)
        check("line_no points at the real line", bool(ln) and ln[0] == nlines,
              f"recorded {ln and ln[0]} vs file has {nlines} lines")
        check("no bad_lines reported", "bad_lines" not in r.stdout, r.stdout.strip()[:80])
        c.close()

        # --- a session split across two files (--resume)
        f2 = proj / slug / f"{sid}-part2.jsonl"
        f2.write_text(_rec(type="user", sessionId=sid, timestamp=_ts(0),
                           cwd=WS + "/outlook_mailer",
                           message={"content": "second-file-token ต่อจากไฟล์แรก"}))
        run([PY, EXTRACT, "--quiet"], env)
        c = sqlite3.connect(db)
        check("session spanning two files stays ONE session",
              c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1)
        check("counts aggregate across both files",
              c.execute("SELECT n_user FROM sessions").fetchone()[0]
              == c.execute("SELECT COUNT(*) FROM turns WHERE role='user'").fetchone()[0])
        check("second file's project is not lost",
              c.execute("SELECT project FROM sessions").fetchone()[0] == "outlook_mailer")
        c.close()

        # --- show --last must land on the newest turn, not the oldest
        out = run([PY, RECALL, "--no-refresh", "show", sid, "--last", "--context", "0"], env)
        check("show --last reaches the end of the session",
              "second-file-token" in out.stdout,
              (out.stdout + out.stderr).strip()[-90:])
        out_head = run([PY, RECALL, "--no-refresh", "show", sid, "--context", "0"], env)
        check("show without --last still starts at the beginning",
              "second-file-token" not in out_head.stdout)
        out_all = run([PY, RECALL, "--no-refresh", "show", sid, "--all"], env)
        check("turns from two files are shown in time order, not per-file seq order",
              out_all.stdout.index("qa-real-token") < out_all.stdout.index("second-file-token"))

        # --- subagent transcripts
        sub = proj / slug / sid / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text(
            _rec(type="assistant", sessionId=sid, timestamp=_ts(0), isSidechain=True,
                 cwd=WS + "/outlook_mailer",
                 message={"content": [{"type": "text", "text": "subagent-report-token"}]}))
        run([PY, EXTRACT, "--quiet"], env)
        c = sqlite3.connect(db)
        check("subagent transcript is indexed",
              c.execute("SELECT COUNT(*) FROM turns WHERE text LIKE"
                        " '%subagent-report-token%'").fetchone()[0] == 1)
        check("subagent turn is flagged is_sidechain",
              c.execute("SELECT is_sidechain FROM turns WHERE text LIKE"
                        " '%subagent-report-token%'").fetchone()[0] == 1)
        c.close()
        out = run([PY, RECALL, "--no-refresh", "search", "subagent-report-token", "--json"], env)
        check("subagent text hidden from default search",
              not json.loads(out.stdout or "{}").get("results"))

        # --- forget must survive the next extract
        run([PY, RECALL, "--no-refresh", "forget", sid], env)
        check("forget empties the session", _count(db, "sessions") == 0)
        run([PY, EXTRACT, "--quiet"], env)
        check("forget SURVIVES the next extract run", _count(db, "sessions") == 0,
              f"sessions={_count(db, 'sessions')}")
        check("forget leaves no session id behind in meta",
              _scalar(db, "SELECT COUNT(*) FROM meta WHERE v LIKE '%%%s%%'" % sid[:8]) == 0)
        check("forget did not delete the transcripts", f1.exists() and f2.exists())

        # --- the core value claim: content outlives the transcript
        proj2 = tmp / "projects2"
        db2 = tmp / "arch.db"
        env2 = dict(os.environ, RECALL_PROJECTS_DIR=str(proj2), RECALL_DB=str(db2))
        (proj2 / slug).mkdir(parents=True)
        sid2 = "eeeeeeee-1111-2222-3333-444444444444"
        f3 = proj2 / slug / f"{sid2}.jsonl"
        f3.write_text(_rec(type="user", sessionId=sid2, timestamp=_ts(60),
                           cwd=WS + "/outlook_mailer",
                           message={"content": "ความรู้ที่จะหายถ้าไม่เก็บ archived-proof-token"}))
        run([PY, EXTRACT, "--full"], env2)
        f3.unlink()                                   # what cleanupPeriodDays does at 30 days
        run([PY, EXTRACT, "--quiet"], env2)
        out = run([PY, RECALL, "--no-refresh", "search", "archived-proof-token", "--json"], env2)
        res = json.loads(out.stdout or "{}").get("results") or []
        check("content is still searchable after the transcript is deleted", bool(res))
        check("and is reported as archived", bool(res) and res[0]["jsonl_exists"] is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _count(db, table) -> int:
    return _scalar(db, f"SELECT COUNT(*) FROM {table}")


def _scalar(db, sql) -> int:
    c = sqlite3.connect(db)
    try:
        return c.execute(sql).fetchone()[0]
    finally:
        c.close()


# ---------------------------------------------------------------- unit

def unit_tests() -> None:
    print("\n=== UNIT ===")
    for s in SECRETS:
        red = L.redact(s)
        check(f"redactor >= detector for {s[:22]!r}", not L.detector_hits(red), red[:40])
    r = subprocess.run(
        [PY, "-c", "import sys; sys.path.insert(0, %r); import recall_lib as L;"
                   " print(L._dir_by_slug)" % str(HERE)],
        capture_output=True, text=True, timeout=60)
    check("import does NOT scan the workspace (scheduled-run hang regression)",
          r.stdout.strip() == "None", r.stdout.strip()[:40] or r.stderr.strip()[:60])
    check("data dir is outside the code dir", L.DATA_DIR != HERE and HERE not in L.DB_PATH.parents,
          str(L.DB_PATH))
    check("embeddings are off unless RECALL_EMBED_URL is set",
          subprocess.run([PY, "-c", "import sys; sys.path.insert(0, %r); import recall_lib as L;"
                          " print(L.EMBED_ENABLED)" % str(HERE)], capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items() if k != "RECALL_EMBED_URL"}
                         ).stdout.strip() == "False")
    for bad, why in [
        ("AKIATUH7WKGDQ3EOB23R", "aws access key id found live in the archive"),
        ('"api_key": "SUPERSECRET"', "json form missed by the =/: -only pattern"),
        ("'password': 'hunter2'", "json form, single quotes"),
        ("ghp_abcdefghijklmnopqrstuvwxyz12", "github token"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig", "jwt"),
    ]:
        red = L.redact(bad)
        check(f"redacts {why}", not L.detector_hits(red) and "[REDACTED]" in red, red[:44])
    for keep, why in [
        ("Bearer token ของ API", "the prose 'Bearer token' is not a credential"),
        ("go_live_date = 2026-10-01", "go_live_* on a go-live project"),
        ("is_live_now flag", "_live_ inside an identifier"),
        ("shots/11_live_stale.png", "screenshot filename"),
        ("task-manager", "sk- inside a word"),
        ("Password:\nsyntax error on line 3", "separator must not cross a newline"),
    ]:
        red = L.redact(keep)
        check(f"does NOT corrupt: {why}", red == keep, f"{keep[:24]!r} -> {red[:34]!r}")
    check("UTC evening maps to the next local (UTC+7) day",
          L.local_date("2026-09-20T17:00:00Z") == "2026-09-21",
          L.local_date("2026-09-20T17:00:00Z"))
    check("UTC morning keeps the same local (UTC+7) day",
          L.local_date("2026-09-20T03:00:00Z") == "2026-09-20")
    t0 = time.time()
    L.clean_text("<system-reminder>" * 20000 + "x" * 100000)
    dt = time.time() - t0
    check("clean_text is not quadratic on unclosed tags", dt < 2.0, f"{dt:.2f}s")
    check("clean_text drops a system-reminder",
          "X" not in L.clean_text("<system-reminder>X</system-reminder> keep"))
    check("clean_text keeps the real text",
          "keep" in L.clean_text("<system-reminder>X</system-reminder> keep"))
    check("scratchpad path -> parent project",
          L.project_for(f"/private/tmp/claude-501/{L._slug(WS + '/alpha_app')}/x/scratchpad", "z")
          == "alpha_app")
    check("workspace subdir -> folder name",
          L.project_for(WS + "/gamma_svc", "z") == "gamma_svc")
    check("workspace root -> its folder name", L.project_for(WS, "z") == "Workspace")
    check("home -> ~", L.project_for(str(Path.home()), "z") == "~")


def main() -> int:
    print("recall test suite")
    unit_tests()
    fixture_tests()
    memory_fixture_tests()
    review_fix_tests()
    drift_tests()
    semantic_tests()
    qa_fixture_tests()
    print("\n=== PRIVACY ===")
    r = subprocess.run([PY, str(ROOT / "tools" / "check_private.py")],
                       capture_output=True, text=True)
    check("repo has no private references (tools/check_private.py)", r.returncode == 0,
          (r.stdout.strip().splitlines() or [""])[-1][:120])
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    bad = [n for n, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(bad)}/{len(_results)} passed")
    if bad:
        print("FAILED:")
        for n in bad:
            print("   -", n)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
