#!/usr/bin/env python3
"""PostToolUse hook — ask Claude to save before auto-compact arrives (two tiers).

The problem: a long agentic task fills the context until Claude Code compacts it on its own,
and nothing was saved first. After every tool call this hook reads the real context size of the
latest assistant message from the end of the transcript and, once per compact cycle per tier:

  tier 1  >= FULL_PCT  (default 70 %) -> run /savebeforecompact (full save), then keep working
  tier 2  >= QUICK_PCT (default 88 %) -> `quick` save: refresh only ON RESUME + Open threads

When a compact brings the context back under tier 1, the cycle starts over. Facts that happen
after the note (the user's messages, files written, commands run) need no saving at all —
session_resume.py pulls them from the transcript when the session resumes.

Configuration (environment variables, all optional):
  CLAUDE_CONTEXT_WINDOW      context window in tokens (default 200000). Set 1000000 if you use a
                             1M-context model. If a reading ever exceeds the configured window,
                             the hook assumes 1,000,000 for that reading.
  CLAUDE_CONTEXT_FULL_PCT    tier 1 as a fraction (default 0.70)
  CLAUDE_CONTEXT_QUICK_PCT   tier 2 as a fraction (default 0.88)
  CLAUDE_CONTEXT_WARN_TOKENS tier 1 as an absolute token count (overrides FULL_PCT)
Where auto-compact fires depends on the model and the Claude Code version; if it compacts
before tier 2, lower the tiers.

Reads only the last 400 KB of the transcript (~0.02 s on a 95 MB file). Fails open: any error
exits 0 silently — a hook must never break the session it is watching.
"""
import json, os, sys, time

ONE_M = 1_000_000
TAIL_BYTES = 400_000


def _f(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


def thresholds(window):
    full_at = int(_f("CLAUDE_CONTEXT_WARN_TOKENS", 0)) or int(window * _f("CLAUDE_CONTEXT_FULL_PCT", 0.70))
    quick_at = max(int(window * _f("CLAUDE_CONTEXT_QUICK_PCT", 0.88)), full_at + 1)
    return full_at, quick_at


def state_dir():
    """Plugin state must survive plugin updates, so never next to the script (inside the checkout)."""
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not d:
        cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        d = os.path.join(cfg, "plugin-state", "savebeforecompact")
    return os.path.join(d, "state")


def last_context_tokens(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]                     # the first line may be cut in half
    for raw in reversed(lines):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        u = (d.get("message") or {}).get("usage") or {}
        if u:
            # A turn that ran server-side sub-calls (e.g. an advisor tool) reports top-level usage
            # SUMMED over every iteration — measured once at 718k while the real context was 361k.
            # The context size is the LAST main ("message") iteration.
            its = [i for i in (u.get("iterations") or []) if i.get("type") == "message"]
            if its:
                u = its[-1]
            return (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0))
    return None


def _level(flag):
    try:
        with open(flag, encoding="utf-8") as f:
            return int(f.read().split()[0])
    except Exception:
        return 0


def main():
    inp = json.load(sys.stdin.buffer)          # bytes -> UTF-8 always (Windows stdin is cp1252)
    # Subagents fire PostToolUse too, but their input points at the PARENT's transcript, so they
    # would read the parent's context size and be told to save. Only the main session saves.
    if inp.get("agent_id") or inp.get("agent_type"):
        return 0
    sid, tpath = inp.get("session_id"), inp.get("transcript_path")
    if not sid or not tpath or not os.path.isfile(tpath):
        return 0
    tokens = last_context_tokens(tpath)
    if tokens is None:
        return 0

    window = int(_f("CLAUDE_CONTEXT_WINDOW", 200_000))
    if tokens > window:                        # the configured window is clearly too small
        window = max(window, ONE_M)
    full_at, quick_at = thresholds(window)

    sdir = state_dir()
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "context-watch-last-run"), "w", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} session={sid} tokens={tokens}"
                f" window={window} full_at={full_at} quick_at={quick_at}\n")
    flag = os.path.join(sdir, f"context-warned-{sid}")

    if tokens < full_at:
        if os.path.exists(flag):              # compacted, context dropped -> new cycle
            os.remove(flag)
        return 0
    want = 2 if tokens >= quick_at else 1
    done = _level(flag)
    if done >= want:                          # this tier already warned this cycle
        return 0
    with open(flag, "w", encoding="utf-8") as f:
        f.write(f"{want} {tokens}")
    for name in os.listdir(sdir):             # sweep flags of old sessions (> 7 days)
        fp = os.path.join(sdir, name)
        if name.startswith("context-warned-") and time.time() - os.path.getmtime(fp) > 7 * 86400:
            os.remove(fp)

    pct = f"{tokens / window:.0%}"
    if want == 1 or done == 0:
        # tier 1 — or a single tool result jumped straight past tier 2: no note this cycle yet -> full save
        msg = (
            f"CONTEXT {tokens:,} tokens ({pct} of {window:,}) - past the save threshold ({full_at:,}).\n"
            "Before continuing the task: invoke the skill `savebeforecompact:savebeforecompact` with the "
            "Skill tool now (full save). Do not stop to ask the user.\n"
            "Make 'ON RESUME' item 1 the next concrete step of the task in progress (command / file / "
            "the exact point reached), then return to the task where you left it.\n"
            "Work done after this is not lost: after a compact, the resume hook pulls the user's messages, "
            "files written and commands run since the note from the transcript."
        )
        sysmsg = f"context {tokens:,} ({pct}) - asked for a full save before auto-compact"
    else:
        msg = (
            f"CONTEXT {tokens:,} tokens ({pct} of {window:,}) - auto-compact is close (tier 2, {quick_at:,}).\n"
            "QUICK SAVE now, then continue the task: invoke `savebeforecompact:savebeforecompact` with the "
            "argument `quick` (rewrite only ON RESUME + Open threads in this session's note - no probe, "
            "no memory files, no CLAUDE.md, no long report).\n"
            "Facts after the note (user messages, files, commands) are recovered automatically - the quick "
            "save records *judgment*: what the next step is, what is being tried, what failed."
        )
        sysmsg = f"context {tokens:,} ({pct}) - asked for a quick save before auto-compact"
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg},
        "systemMessage": sysmsg,
    }, ensure_ascii=True))                    # ASCII only: a Windows cp1252 console cannot silence it
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)                           # a hook must never break the session it runs in
