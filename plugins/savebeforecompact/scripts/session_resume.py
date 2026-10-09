#!/usr/bin/env python3
"""SessionStart hook — load the session resume note back into context after /compact or --resume.

Fires only on source = compact | resume (never on startup / clear, so an old note cannot
pollute fresh work). Prints nothing when there is no note.

Pairs with the /savebeforecompact skill, which writes the note. On top of the note it appends
the facts that happened AFTER the note was written, read straight from the transcript: the
user's messages verbatim, files written, Bash commands, and Claude's last message. That part
costs nothing while working and works the same for a manual /compact and a forced auto-compact.
"""
import glob, json, os, re, sys, time

MAX_CHARS = 24000          # cap an abnormally long note
FIRE_ON   = ("compact", "resume")
DELTA_MAX = 14000          # character budget for "work after the note"

_REDACT = [
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]+"), "[REDACTED]"),
    # the whole assignment goes, label included: the skill's detector grep flags `api_key =` itself,
    # so `api_key = [REDACTED]` copied into a note would still fail its Step 5 check
    (re.compile(r"""api[_-]?key["']?[ \t]*[=:][ \t]*["']?[^\s"']+""", re.I), "[REDACTED-CREDENTIAL]"),
    (re.compile(r"""password["']?[ \t]*[=:][ \t]*["']?[^\s"']+""", re.I), "[REDACTED-PASSWORD]"),
    (re.compile(r"""(\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)=["']?)(?![/~$])[^\s"']{8,}"""),
     r"\1[REDACTED]"),
    (re.compile(r"(Bearer[ \t]+)(?![Tt]oken\b)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?<![A-Za-z0-9])(?:sk|pk|rk|whsec)_live_[A-Za-z0-9]+"), "[REDACTED]"),
    (re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+"), "[REDACTED]"),
]
_TAGS = ("system-reminder", "local-command-stdout", "local-command-caveat", "persisted-output",
         "task-notification", "command-message", "command-args")


def _redact(t):
    for pat, rp in _REDACT:
        t = pat.sub(rp, t)
    return t


def _strip_tags(t):
    # a slash command the user typed: keep the command itself (`/recall:recall`), drop the tags
    t = re.sub(r"<command-name>(.*?)</command-name>", r"\1 ", t, flags=re.S)
    for tag in _TAGS:
        o, c = f"<{tag}>", f"</{tag}>"
        while True:
            i = t.find(o)
            if i < 0:
                break
            j = t.find(c, i)
            if j < 0:
                break
            t = t[:i] + t[j + len(c):]
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _epoch(ts):
    import datetime
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _short(t, n):
    t = t.strip()
    return t if len(t) <= n else t[:n] + f" ...[+{len(t) - n} chars]"


def _tail_hint(inp):
    """How to see where the conversation ended, without assuming any other plugin is installed."""
    sid = inp.get("session_id", "<session_id>")
    tp = inp.get("transcript_path") or "<transcript>"
    return ("To see where the work stopped: if the `recall` plugin is installed, run "
            f"`/recall:recall show {sid} --last`; otherwise read the last user/assistant records of the "
            f"transcript `{tp}` (JSON lines; skip tool results).\n")


def delta_since(tpath, since):
    """Facts from the transcript after epoch `since`: user messages verbatim, files written/edited,
    Bash commands, the assistant's last text. Returns a string ('' = nothing after the note)."""
    if not tpath or not os.path.isfile(tpath) or since is None:
        return ""
    users, files, cmds, last_text, n_after = [], [], [], "", 0
    with open(tpath, "rb") as f:
        for raw in f:
            if b'"timestamp"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            t = d.get("type")
            if t not in ("user", "assistant") or d.get("isSidechain"):
                continue
            ep = _epoch(d.get("timestamp") or "")
            if ep is None or ep <= since:
                continue
            # compact summaries and system-inserted text (skill bodies etc.) are not what the user typed
            if d.get("isCompactSummary") or d.get("isMeta") or d.get("isVisibleInTranscriptOnly"):
                continue
            n_after += 1
            content = (d.get("message") or {}).get("content")
            if t == "user":
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(b.get("text") or "" for b in content
                                     if isinstance(b, dict) and b.get("type") == "text")
                else:
                    text = ""
                text = _strip_tags(text)
                if text and not text.startswith("[Request interrupted"):
                    stamp = time.strftime("%m-%d %H:%M", time.localtime(ep))
                    users.append(f"[{stamp}] " + _short(_redact(text), 1500))
                continue
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    last_text = b["text"]
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    name = b.get("name") or ""
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit") and fp:
                        tag = f"{name}: {fp}"
                        if tag in files:
                            files.remove(tag)
                        files.append(tag)
                    elif name == "Bash" and inp.get("command"):
                        cmds.append(_short(_redact(" ".join(inp["command"].split())), 300))
    if not n_after:
        return ""
    files, cmds = files[-40:], cmds[-30:]

    def render(us):
        parts = ["", "=" * 70,
                 "WORK AFTER THE NOTE - pulled from the transcript automatically (facts, not a summary)",
                 f"since {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))} - {n_after} messages",
                 "Where this contradicts the note above, trust this part - it is newer. 'ON RESUME' in the "
                 "note may be stale: read the latest user message and the assistant's last message below "
                 "to see where the work really stands."]
        if us:
            parts += ["", f"User messages (verbatim, latest {len(us)}):"] + [f"  {u}" for u in us]
        if files:
            parts += ["", f"Files written/edited ({len(files)}):"] + [f"  {x}" for x in files]
        if cmds:
            parts += ["", f"Bash commands (latest {len(cmds)}, shortened, secrets redacted):"] + [f"  $ {c}" for c in cmds]
        if last_text:
            parts += ["", "The assistant's last message before the stop:", _short(_redact(last_text), 2500)]
        return "\n".join(parts) + "\n"

    keep = users[-25:]
    out = render(keep)
    while len(out) > DELTA_MAX and len(keep) > 3:     # drop the oldest user messages first
        keep = keep[1:]
        out = render(keep)
    if len(out) > DELTA_MAX:
        out = out[:DELTA_MAX] + "\n...[cut - read the end of the transcript for the rest]\n"
    return out


def cycle_start(tpath):
    """Start of this work cycle = the latest earlier compact_boundary (ignoring one that happened in
    the last 10 minutes, i.e. the compact that just fired). Never compacted -> the timestamp of the
    transcript's first record, minus 1 s so that record counts. Returns epoch or None.

    Not the file's creation time: Linux has none, Windows reports ctime, and a copied/restored file
    gets a new one — which made "work after the note" come out empty when there was real work."""
    if not tpath or not os.path.isfile(tpath):
        return None
    stamps, first = [], None
    with open(tpath, "rb") as f:
        for raw in f:
            if b'"timestamp"' not in raw:
                continue
            boundary = b'"compact_boundary"' in raw
            if first is not None and not boundary:
                continue
            try:
                ep = _epoch(json.loads(raw).get("timestamp", ""))
            except Exception:
                continue
            if ep is None:
                continue
            if first is None:
                first = ep
            if boundary:
                stamps.append(ep)
    older = [t for t in stamps if time.time() - t > 600]
    if older:
        return max(older)
    return first - 1 if first is not None else None


def stale_hint(inp, note_mtime):
    """When the note predates this work cycle (an auto-compact during a long task with no save)."""
    if inp.get("source") != "compact":
        return ""
    start = cycle_start(inp.get("transcript_path"))
    if note_mtime is not None and (start is None or note_mtime >= start):
        return ""
    return ("WARNING: this note is older than the work cycle that was just compacted "
            "(probably an auto-compact before any save).\n" + _tail_hint(inp) +
            "The compact summary may have dropped verbatim commands and the user's latest instructions; "
            "the transcript has them.\n")


def _same_dir(a, b):
    """Windows paths arrive with / or \\ and in either case (C: vs c:) -> normcase before comparing."""
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def own_note(tpath, mem):
    """The note THIS session wrote last — from Write/Edit tool_use records touching session_resume_*.md.
    Beats picking by mtime, which another session (or a one-line edit) can bump."""
    if not tpath or not os.path.isfile(tpath):
        return None
    found = None
    pat = re.compile(r"session_resume_[A-Za-z0-9_-]+\.md")
    with open(tpath, "rb") as f:
        for raw in f:
            if b"session_resume_" not in raw or b'"tool_use"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except Exception:
                continue
            for blk in (d.get("message") or {}).get("content") or []:
                if not isinstance(blk, dict) or blk.get("type") != "tool_use":
                    continue
                if blk.get("name") not in ("Write", "Edit", "MultiEdit"):
                    continue
                fp = (blk.get("input") or {}).get("file_path", "")
                m = pat.search(os.path.basename(fp))
                if m and _same_dir(os.path.dirname(os.path.abspath(fp)), mem):
                    found = fp
    return found if found and os.path.isfile(found) and os.path.getsize(found) > 0 else None


def origin_of(path):
    """originSessionId in the note's frontmatter (Claude Code adds it to memory files) - None if absent."""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.read(2000)
    except Exception:
        return None
    m = re.search(r"^\s*originSessionId:\s*([0-9a-f-]{8,})", head, re.M)
    return m.group(1) if m else None


def _when(p):
    return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))


def foreign_only(inp, notes):
    """Compacted, this session never wrote a note, and the newest note says it belongs to another
    session -> do NOT load it as RESUMING (it would steer the work to the wrong task)."""
    listing = "\n".join(f"  - {os.path.basename(n)}  ({_when(n)})" for n in notes)
    msg = ("WARNING: this session was compacted but never wrote a session resume note of its own.\n"
           "The notes in this project belong to other sessions (other work) - they were NOT loaded; "
           "do not act on them:\n"
           f"{listing}\n" + _tail_hint(inp) + "If it is still unclear, ask the user.\n")
    msg += delta_since(inp.get("transcript_path"), cycle_start(inp.get("transcript_path")))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": msg},
                      "systemMessage": "session resume: no note from this session - other sessions' notes not loaded"},
                     ensure_ascii=True))
    return 0


def emit_hint_only(inp):
    hint = stale_hint(inp, None)
    if hint:
        hint += delta_since(inp.get("transcript_path"), cycle_start(inp.get("transcript_path")))
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": hint}}, ensure_ascii=True))
    return 0


def memory_dir(cwd):
    """<config>/projects/<slug>/memory for cwd or the nearest parent that has one.

    A session may run in a subfolder of the project (slug differs from the project root's), so walk
    up to 6 parents — otherwise the note is written but never read back, silently."""
    cands, d = [], os.path.abspath(cwd)
    for _ in range(7):
        cands.append(d)
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    roots = [os.environ.get("CLAUDE_CONFIG_DIR"), os.path.join(os.path.expanduser("~"), ".claude")]
    for c in cands:
        slug = re.sub(r"[^A-Za-z0-9]", "-", c)
        for root in roots:
            if root:
                cand = os.path.join(os.path.expanduser(root), "projects", slug, "memory")
                if os.path.isdir(cand):
                    return cand
    return ""


def main():
    try:
        inp = json.load(sys.stdin.buffer)   # bytes -> UTF-8 always (Windows stdin is cp1252)
    except Exception:
        return 0

    src = inp.get("source", "")
    if src not in FIRE_ON:
        return 0

    mem = memory_dir(inp.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if not mem:
        return emit_hint_only(inp)

    notes = [f for f in glob.glob(os.path.join(mem, "session_resume_*.md"))
             if os.path.getsize(f) > 0]
    if not notes:
        return emit_hint_only(inp)
    notes.sort(key=os.path.getmtime, reverse=True)
    mine = own_note(inp.get("transcript_path"), mem)
    sid = inp.get("session_id")
    if not mine and sid:                  # fallback: the frontmatter says it is this session's
        mine = next((n for n in notes if origin_of(n) == sid), None)
    if mine:                              # the stream this session wrote wins over mtime
        notes.sort(key=lambda n: not _same_dir(n, mine))
    elif src == "compact" and sid and origin_of(notes[0]) not in (None, sid):
        return foreign_only(inp, notes)   # none of our own, and the newest is certainly someone else's
    newest = notes[0]

    mt = os.path.getmtime(newest)
    age_h = (time.time() - mt) / 3600
    age = f"{age_h*60:.0f} min ago" if age_h < 1 else (
          f"{age_h:.1f} h ago" if age_h < 48 else f"{age_h/24:.1f} days ago")
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(mt))

    with open(newest, encoding="utf-8") as f:
        body = f.read()
    truncated = ""
    if len(body) > MAX_CHARS:
        body = body[:MAX_CHARS]
        truncated = f"\n\n[cut at {MAX_CHARS} chars - read the rest at {newest}]"

    others = ""
    if len(notes) > 1:
        recent = [n for n in notes[1:] if time.time() - os.path.getmtime(n) < 6 * 3600]
        listing = "\n".join(f"  - {os.path.basename(n)}  ({_when(n)})" for n in notes[1:])
        others = f"\n\nOther streams in this project (not loaded - open them yourself if needed):\n{listing}"
        if recent:
            others += ("\n  WARNING: another stream was updated in the last 6 h - if the note below does not "
                       "match the work in progress, ask the user which stream to continue")

    hint = stale_hint(inp, mt)
    # facts after the note: count from when the note was written (or from the start of this cycle if
    # the note is older — the cycle before the previous compact is already in the old summary)
    start = cycle_start(inp.get("transcript_path"))
    delta = delta_since(inp.get("transcript_path"), max(mt, start or 0))
    header = (
        "RESUMING - this session was " + ("compacted" if src == "compact" else "resumed") + ".\n"
        "Read the note below and continue from where the work stopped. Do not re-derive what it already "
        "says, and do not ask the user again about anything it answers.\n\n"
        f"File: {newest}\n"
        f"Written: {stamp} ({age})\n"
        "Written by the /savebeforecompact skill - 'ON RESUME - DO THIS FIRST' is the first thing to do."
        f"{others}\n"
        + (("\n" + hint) if hint else "")
        + "\n" + "=" * 70 + "\n")

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": header + body + truncated + delta,
        },
        "systemMessage": f"session resume loaded: {os.path.basename(newest)} ({age})"
                         + (" + work after the note from the transcript" if delta else ""),
    }, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    # JSON goes out ASCII-only (ensure_ascii=True), so a Windows cp1252 console cannot break it —
    # that used to make the hook silently inject nothing.
    try:
        sys.stderr.reconfigure(errors="backslashreplace")
    except Exception:
        pass
    try:
        sys.exit(main())
    except Exception as e:            # a hook must never break the session
        print(f"session_resume hook: {e}", file=sys.stderr)
        sys.exit(1)                   # non-blocking
