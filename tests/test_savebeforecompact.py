#!/usr/bin/env python3
"""Test suite for the savebeforecompact plugin's two hooks.   python3 tests/test_savebeforecompact.py

Every test drives the real hook scripts as subprocesses — JSON on stdin, exactly as Claude Code
calls them — against synthetic transcripts and memory directories in a temp dir
(CLAUDE_CONFIG_DIR / CLAUDE_PLUGIN_DATA point there). Your own ~/.claude is never read or written.

The redaction test is arranged so it cannot be fudged: the ORACLE is the detector grep the skill
runs on every file it writes (Step 5). A redactor narrower than the detector fails this suite.
Fix the redactor — never the detector.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fixtures carry Thai text and emoji. On a Thai-locale Windows Python defaults to cp874 and the
# subprocess output fails to decode. Re-run in UTF-8 mode (subprocess, not execv: on Windows
# execv returns before the child finishes and loses the exit code).
if not sys.flags.utf8_mode:
    os.environ["PYTHONUTF8"] = "1"
    raise SystemExit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "savebeforecompact"
RESUME = PLUGIN / "scripts" / "session_resume.py"
WATCH = PLUGIN / "scripts" / "context_watch.py"

SANDBOX = Path(tempfile.mkdtemp(prefix="sbc-suite-"))
CONFIG = SANDBOX / "config"
DATA = SANDBOX / "plugin-data"
# POSIX form, not "Asia/Bangkok": the Windows CRT misparses IANA names as UTC.
os.environ["TZ"] = "ICT-7"
if hasattr(time, "tzset"):
    time.tzset()

DETECTOR = re.compile(r"sk-[A-Za-z0-9]|api[_-]?key *[=:]|Bearer [A-Za-z0-9._-]{12,}|password *[=:]"
                      r"|_live_[A-Za-z0-9]|eyJ[A-Za-z0-9_-]{8,}", re.I)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   -- {detail}"))
    return ok


def env(**extra) -> dict:
    e = dict(os.environ)
    for k in ("CLAUDE_CONTEXT_WINDOW", "CLAUDE_CONTEXT_FULL_PCT", "CLAUDE_CONTEXT_QUICK_PCT",
              "CLAUDE_CONTEXT_WARN_TOKENS", "CLAUDE_PROJECT_DIR"):
        e.pop(k, None)
    e.update(CLAUDE_CONFIG_DIR=str(CONFIG), CLAUDE_PLUGIN_DATA=str(DATA), PYTHONUTF8="1")
    e.update({k: str(v) for k, v in extra.items()})
    return e


def run(script: Path, payload: dict, **extra) -> tuple[int, dict | None, str]:
    p = subprocess.run([sys.executable, str(script)], input=json.dumps(payload).encode("utf-8"),
                       capture_output=True, env=env(**extra), timeout=60)
    out = p.stdout.decode("utf-8", "replace").strip()
    try:
        return p.returncode, (json.loads(out) if out else None), out
    except json.JSONDecodeError:
        return p.returncode, None, out


def ctx(res) -> str:
    return (((res or {}).get("hookSpecificOutput") or {}).get("additionalContext")) or ""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


NOW = datetime.now(timezone.utc)


def ago(minutes: float) -> str:
    return iso(NOW - timedelta(minutes=minutes))


def project(name: str) -> tuple[Path, Path]:
    """A project folder and its memory dir, slugged the way Claude Code does it."""
    proj = SANDBOX / "work" / name
    proj.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(proj.resolve()))
    mem = CONFIG / "projects" / slug / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    return proj.resolve(), mem


def note(mem: Path, name: str, body: str, origin: str | None = None, mtime_min_ago: float = 30) -> Path:
    fm = f"---\nname: {name[:-3]}\ndescription: test\n"
    if origin:
        fm += f"metadata:\n  originSessionId: {origin}\n"
    p = mem / name
    p.write_text(fm + "---\n\n" + body + "\n", encoding="utf-8")
    t = time.time() - mtime_min_ago * 60
    os.utime(p, (t, t))
    return p


def user(ts: str, text, **kw) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}, **kw}


def asst(ts: str, blocks, usage=None, **kw) -> dict:
    m = {"role": "assistant", "content": blocks}
    if usage is not None:
        m["usage"] = usage
    return {"type": "assistant", "timestamp": ts, "message": m, **kw}


def tool(name: str, **inp) -> dict:
    return {"type": "tool_use", "id": "t", "name": name, "input": inp}


def transcript(name: str, records: list[dict]) -> Path:
    p = SANDBOX / "transcripts" / f"{name}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


# ------------------------------------------------------------------ session_resume.py

def resume_tests() -> None:
    proj, mem = project("alpha")
    sid = "aaaaaaaa-1111-2222-3333-444444444444"

    # 1. startup / clear never fire
    note(mem, "session_resume_current.md", "ON RESUME: run make test")
    for src in ("startup", "clear"):
        rc, res, out = run(RESUME, {"source": src, "cwd": str(proj), "session_id": sid})
        check(f"resume: silent on {src}", rc == 0 and out == "", out[:200])

    # 2. no memory dir at all -> silent
    rc, res, out = run(RESUME, {"source": "resume", "cwd": str(SANDBOX / "nowhere"), "session_id": sid})
    check("resume: silent with no memory dir", rc == 0 and out == "", out[:200])

    # 3. own note (written via the transcript) beats a newer note from another stream
    own = note(mem, "session_resume_billing.md", "ON RESUME: billing next step 42", mtime_min_ago=20)
    note(mem, "session_resume_docs.md", "ON RESUME: docs stream", mtime_min_ago=1)
    tp = transcript("own", [
        user(ago(120), "start billing work"),
        asst(ago(25), [tool("Write", file_path=str(own), content="x")]),
    ])
    rc, res, out = run(RESUME, {"source": "compact", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    c = ctx(res)
    check("resume: own note wins over a newer stream", "billing next step 42" in c and "docs stream" not in c.split("=" * 70)[1] if "=" * 70 in c else False, c[:300])
    check("resume: other streams listed, recent one flagged",
          "session_resume_docs.md" in c and "last 6 h" in c, c[:600])
    check("resume: output is ASCII-only JSON", out.isascii(), "non-ASCII in stdout")

    # 4. Windows-style path in the transcript (backslashes, other case) still counts as own note
    win_path = str(own).replace("/", "\\") if os.name == "nt" else str(own)
    tp = transcript("own_win", [user(ago(60), "x"), asst(ago(25), [tool("Edit", file_path=win_path)])])
    rc, res, out = run(RESUME, {"source": "compact", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    check("resume: Edit counts as writing the note", "billing next step 42" in ctx(res), ctx(res)[:200])

    # 5. originSessionId fallback when the transcript has no Write of the note
    shutil.rmtree(mem); mem.mkdir(parents=True)
    note(mem, "session_resume_api.md", "ON RESUME: api via origin", origin=sid, mtime_min_ago=40)
    note(mem, "session_resume_other.md", "ON RESUME: someone else", origin="bbbbbbbb-0000", mtime_min_ago=2)
    tp = transcript("origin", [user(ago(90), "hello")])
    rc, res, out = run(RESUME, {"source": "compact", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    check("resume: originSessionId fallback picks this session's note", "api via origin" in ctx(res), ctx(res)[:300])

    # 6. foreign-only: compacted, never wrote a note, newest belongs to another session -> load nothing
    shutil.rmtree(mem); mem.mkdir(parents=True)
    note(mem, "session_resume_current.md", "ON RESUME: FOREIGN TASK", origin="cccccccc-9999", mtime_min_ago=5)
    rc, res, out = run(RESUME, {"source": "compact", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    c = ctx(res)
    check("resume: foreign note is not loaded", "FOREIGN TASK" not in c and "NOT loaded" in c, c[:400])
    check("resume: foreign case points at the transcript", str(tp) in c and "recall:recall show" in c, c[:600])

    # 7. subfolder cwd walks up to the project's memory dir
    shutil.rmtree(mem); mem.mkdir(parents=True)
    note(mem, "session_resume_current.md", "ON RESUME: from subfolder", mtime_min_ago=5)
    sub = proj / "a" / "b"
    sub.mkdir(parents=True, exist_ok=True)
    rc, res, out = run(RESUME, {"source": "resume", "cwd": str(sub), "session_id": sid})
    check("resume: subfolder cwd finds the project's note", "from subfolder" in ctx(res), out[:300])

    # 8. delta: facts after the note, excluding compact summary / meta / sidechain / reminders
    shutil.rmtree(mem); mem.mkdir(parents=True)
    n = note(mem, "session_resume_current.md", "ON RESUME: old plan", mtime_min_ago=30)
    tp = transcript("delta", [
        user(ago(200), "first message before the note"),
        asst(ago(31), [tool("Write", file_path=str(n), content="x")]),
        user(ago(20), "<command-name>/deploy</command-name><command-args></command-args> ใช้ staging ก่อนนะ"),
        user(ago(19), "compact summary text", isCompactSummary=True),
        user(ago(18), "skill body text", isMeta=True),
        user(ago(17), [{"type": "text", "text": "keep this <system-reminder>HIDDEN REMINDER</system-reminder> part"}]),
        asst(ago(16), [tool("Bash", command="curl -H 'Authorization: Bearer abcdefghijklmnop123' https://x")]),
        asst(ago(15), [tool("Edit", file_path="/srv/app/config.py")]),
        asst(ago(14), [{"type": "text", "text": "subagent chatter"}], isSidechain=True),
        asst(ago(13), [{"type": "text", "text": "Deployed to staging; next is prod."}]),
    ])
    rc, res, out = run(RESUME, {"source": "compact", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    c = ctx(res)
    d = c.split("WORK AFTER THE NOTE", 1)[1] if "WORK AFTER THE NOTE" in c else ""
    check("delta: present", bool(d), c[-500:])
    check("delta: user message verbatim incl. Thai + slash command",
          "/deploy" in d and "ใช้ staging ก่อนนะ" in d, d[:500])
    check("delta: message before the note excluded", "first message before the note" not in d, d[:300])
    check("delta: compact summary / meta excluded",
          "compact summary text" not in d and "skill body text" not in d, d[:500])
    check("delta: system-reminder stripped, rest kept", "HIDDEN REMINDER" not in d and "keep this" in d, d[:500])
    check("delta: sidechain excluded", "subagent chatter" not in d, d[:500])
    check("delta: edited file listed", "/srv/app/config.py" in d, d[:500])
    check("delta: bearer token redacted", "abcdefghijklmnop123" not in d and "[REDACTED]" in d, d[:600])
    check("delta: last assistant text", "Deployed to staging; next is prod." in d, d[-400:])

    # 9. stale note (older than this cycle's start) -> warning + delta from cycle start
    shutil.rmtree(mem); mem.mkdir(parents=True)
    note(mem, "session_resume_current.md", "ON RESUME: ancient", mtime_min_ago=60 * 24)
    tp = transcript("stale", [
        user(ago(300), "very first record of this session"),
        asst(ago(100), [{"type": "text", "text": "working"}]),
    ])
    rc, res, out = run(RESUME, {"source": "compact", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    c = ctx(res)
    check("stale: warning shown", "older than the work cycle" in c, c[:500])
    check("stale: first record of the session is included (start - 1 s)",
          "very first record of this session" in c, c[-800:])

    # 10. previous compact boundary (> 10 min ago) bounds the delta
    tp = transcript("boundary", [
        user(ago(300), "before the previous compact"),
        {"type": "system", "subtype": "compact_boundary", "timestamp": ago(200)},
        user(ago(150), "after the previous compact"),
        {"type": "system", "subtype": "compact_boundary", "timestamp": ago(1)},
    ])
    rc, res, out = run(RESUME, {"source": "compact", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    c = ctx(res)
    check("boundary: work before the previous compact excluded",
          "before the previous compact" not in c and "after the previous compact" in c, c[-600:])

    # 11. delta budget: huge history stays under the cap and keeps the newest message
    recs = [user(ago(200 - i * 0.1), f"message {i} " + "x" * 1400) for i in range(60)]
    recs.append(user(ago(1), "THE NEWEST MESSAGE"))
    tp = transcript("big", recs)
    shutil.rmtree(mem); mem.mkdir(parents=True)
    note(mem, "session_resume_current.md", "ON RESUME: x", mtime_min_ago=250)
    rc, res, out = run(RESUME, {"source": "resume", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    c = ctx(res)
    d = c.split("WORK AFTER THE NOTE", 1)[1] if "WORK AFTER THE NOTE" in c else ""
    check("delta: capped near 14k chars", 0 < len(d) <= 14200, f"len={len(d)}")
    check("delta: newest message survives the cap", "THE NEWEST MESSAGE" in d, d[-300:])

    # 12. a broken line in the transcript does not break the hook
    with open(tp, "a", encoding="utf-8") as f:
        f.write('{"type": "user", "timestamp": "' + ago(0.5) + '", broken\n')
    rc, res, out = run(RESUME, {"source": "resume", "cwd": str(proj), "session_id": sid,
                                "transcript_path": str(tp)})
    check("resume: survives a malformed transcript line", rc == 0 and "THE NEWEST MESSAGE" in ctx(res), out[:200])


# ------------------------------------------------------------------ redactor vs detector

def redaction_tests() -> None:
    sys.path.insert(0, str(PLUGIN / "scripts"))
    import session_resume as R  # noqa: E402
    samples = [
        "export OPENAI=sk-abc123DEF456ghi",
        "api_key = 9f8e7d6c5b4a",
        "API-KEY: zzzzyyyyxxxx",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123",
        "password=hunter2hunter2",
        "stripe pk_live_abcdef123456",
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        'config {"apiKey": "abcdef0123456789"}',
    ]
    for s in samples:
        red = R._redact(s)
        check(f"redact ⊇ detect: {s[:28]!r}", not DETECTOR.search(red), red)
    check("redact: keeps ordinary text", R._redact("Bearer token auth is configured") == "Bearer token auth is configured",
          R._redact("Bearer token auth is configured"))


# ------------------------------------------------------------------ context_watch.py

def usage(tokens: int, advisor: int = 0) -> dict:
    u = {"input_tokens": 10, "cache_read_input_tokens": tokens - 10, "cache_creation_input_tokens": 0}
    if advisor:   # top-level summed over iterations; the last "message" iteration is the real size
        top = dict(u)
        top["cache_read_input_tokens"] += advisor
        top["iterations"] = [{"type": "message", **u}, {"type": "advisor", "input_tokens": advisor},
                             {"type": "message", **u}]
        return top
    return u


def watch(sid: str, tokens: int, advisor: int = 0, **extra):
    tp = transcript(f"w-{sid}-{tokens}", [user(ago(5), "go"),
                                          asst(ago(1), [{"type": "text", "text": "ok"}], usage(tokens, advisor))])
    return run(WATCH, {"session_id": sid, "transcript_path": str(tp), "hook_event_name": "PostToolUse"}, **extra)


def watch_tests() -> None:
    state = DATA / "state"

    rc, res, out = watch("w1", 100_000)
    check("watch: quiet under tier 1 (200k default window)", out == "", out[:200])
    check("watch: last-run file written to plugin data", (state / "context-watch-last-run").is_file(), str(state))

    rc, res, out = watch("w1", 141_000)
    c = ctx(res)
    check("watch: tier 1 at 70 % asks for a full save",
          "savebeforecompact:savebeforecompact" in c and "full save" in c and "quick" not in c.lower().split("full save")[0], c[:300])
    rc, res, out = watch("w1", 150_000)
    check("watch: tier 1 fires once per cycle", out == "", out[:200])

    rc, res, out = watch("w1", 177_000)
    check("watch: tier 2 at 88 % asks for a quick save", "`quick`" in ctx(res), ctx(res)[:300])
    rc, res, out = watch("w1", 180_000)
    check("watch: tier 2 fires once per cycle", out == "", out[:200])

    rc, res, out = watch("w1", 50_000)
    check("watch: dropping under tier 1 resets the cycle", not (state / "context-warned-w1").exists(), "flag still there")
    rc, res, out = watch("w1", 141_000)
    check("watch: new cycle warns again", "full save" in ctx(res), out[:200])

    rc, res, out = watch("w2", 190_000)
    check("watch: jumping straight past tier 2 asks for the FULL save first", "full save" in ctx(res), ctx(res)[:300])

    rc, res, out = watch("w3", 100_000, advisor=400_000)
    check("watch: advisor iterations are not counted", out == "", out[:300])

    rc, res, out = watch("w4", 600_000, CLAUDE_CONTEXT_WINDOW=1_000_000)
    check("watch: 1M window configured -> 60 % is quiet", out == "", out[:200])
    rc, res, out = watch("w4", 710_000, CLAUDE_CONTEXT_WINDOW=1_000_000)
    check("watch: 1M window configured -> 71 % warns", "full save" in ctx(res), out[:200])

    rc, res, out = watch("w5", 400_000)
    check("watch: reading above a 200k window self-corrects to 1M (40 % -> quiet)", out == "", out[:200])

    rc, res, out = watch("w6", 141_000, CLAUDE_CONTEXT_FULL_PCT="0.5", CLAUDE_CONTEXT_QUICK_PCT="0.6")
    check("watch: custom tiers (0.5/0.6) -> 70 % is tier 2 territory, full save first", "full save" in ctx(res), out[:200])

    tp = transcript("sub", [asst(ago(1), [{"type": "text", "text": "x"}], usage(190_000))])
    rc, res, out = run(WATCH, {"session_id": "w7", "transcript_path": str(tp), "agent_id": "a1",
                               "agent_type": "Explore"})
    check("watch: subagent calls are ignored", out == "", out[:200])

    rc, res, out = run(WATCH, {"session_id": "w8", "transcript_path": str(SANDBOX / "missing.jsonl")})
    check("watch: missing transcript -> silent exit 0", rc == 0 and out == "", out[:200])
    p = subprocess.run([sys.executable, str(WATCH)], input=b"not json", capture_output=True, env=env(), timeout=30)
    check("watch: garbage stdin -> exit 0", p.returncode == 0 and not p.stdout, p.stdout[:200])
    check("watch: output is ASCII-only", all(o.isascii() for o in (out,)), "")


# ------------------------------------------------------------------ packaging

def packaging_tests() -> None:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    ss = hooks["SessionStart"][0]
    check("hooks.json: SessionStart matcher is compact|resume", ss["matcher"] == "compact|resume", ss["matcher"])
    cmds = [h["command"] for ev in hooks.values() for m in ev for h in m["hooks"]]
    check("hooks.json: every command falls back from python3 to python",
          all("python3 " in c and "|| python " in c and "${CLAUDE_PLUGIN_ROOT}" in c for c in cmds), "\n".join(cmds))
    for c in cmds:
        script = re.search(r'scripts/([a-z_]+\.py)', c).group(1)
        check(f"hooks.json: {script} exists", (PLUGIN / "scripts" / script).is_file(), script)
    skill = (PLUGIN / "skills" / "savebeforecompact" / "SKILL.md").read_text(encoding="utf-8")
    fm = skill.split("---")[1]
    check("SKILL.md: frontmatter has name + description",
          "name: savebeforecompact" in fm and "description:" in fm, fm[:200])
    desc = re.search(r"^description: (.*)$", fm, re.M).group(1)
    check("SKILL.md: description under 1,024 chars", len(desc) <= 1024, str(len(desc)))
    for src in (RESUME, WATCH):
        text = src.read_text(encoding="utf-8")
        check(f"{src.name}: no Thai left in the code (messages are English)",
              not re.search(r"[฀-๿]", text), "Thai characters found")
    mk = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    names = [p["name"] for p in mk["plugins"]]
    check("marketplace: lists recall and savebeforecompact", names == ["recall", "savebeforecompact"], str(names))
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "check_private.py")], capture_output=True, timeout=60)
    check("check_private: repo is clean", p.returncode == 0, p.stdout.decode("utf-8", "replace")[-600:])


def main() -> int:
    try:
        resume_tests()
        redaction_tests()
        watch_tests()
        packaging_tests()
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
