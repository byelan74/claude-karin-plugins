---
name: savebeforecompact
description: Durably persist everything needed to continue THIS session after /compact — open threads, the exact next step, deliverable status, the user's latest instructions verbatim, the commands that made things work (launch flags, ports, profiles, auth paths), a probe of what is running right now, what worked vs what didn't. Writes memory files + a rolling resume note (one per line of work) that a SessionStart hook loads back after the compact, then reviews CLAUDE.md (proposes a diff by default; edits in place under a 40k-char budget when configured to). Has a `quick` mode (only ON RESUME + Open threads) for the 88 % context warning. Invoke before /compact, when the context-watch hook asks for it, or when the user says "save before compact" / "checkpoint this" / "save the session".
argument-hint: "(optional) stream name, e.g. 'email' — or 'quick' for a 30-second checkpoint of ON RESUME + Open threads only"
---

# /savebeforecompact — persist session state before compacting

Goal: after `/compact` (manual or automatic) or a later `--resume`, nothing important is lost. You —
the same or a future agent — must be able to pick up exactly where the work stopped by reading what
this skill wrote to disk. **This is a save-to-disk operation only: no outward actions** (no sending
mail or messages, no pushing, no deploying, no edits to the files that are the *subject* of the work).

How the pieces fit:
- **this skill** writes the note (judgment: what next, why, what failed);
- **the SessionStart hook** (`session_resume.py`) loads the note after a compact/resume and appends
  the facts that happened after it, straight from the transcript;
- **the PostToolUse hook** (`context_watch.py`) asks for a full save at 70 % of the context window and
  a quick save at 88 %, so long agentic runs save before auto-compact hits.

## Quick mode — argument `quick` (the 88 % checkpoint)

When invoked with `quick` — normally because the context-watch hook said the context is ~88 % full —
do **only this**, then go straight back to the task:

1. Find the resume note **this session** already wrote this cycle (from the 70 % full save).
   If there is none, do the full save instead — a quick save needs a note to update.
2. With Edit, rewrite **only two sections** of it:
   - **ON RESUME — DO THIS FIRST**: the exact next action of the task in progress *right now* (command,
     file, line, decision) — not what it was at the full save.
   - **Open threads**: status of each thread as of now; what is being tried; what was tried and failed
     since the full save, and why.
   Add a verbatim command to *Working setup* only if one started working since the full save.
3. No probe, no memory files, no CLAUDE.md, no report beyond one line ("quick checkpoint saved").
   Run the redaction grep (Step 5) on the note. Resume the task in the same turn.

Why only these two: **facts** that happened after the note — the user's messages verbatim, files
written, commands run, Claude's last message — are recovered from the transcript by the SessionStart
hook. What it cannot recover is *judgment*: the next step, the current hypothesis, the dead ends.

## Where things go

- **Project memory directory**: use the memory directory named in the current session's context
  (Claude Code's auto-memory). If the context names none, use
  `<config>/projects/<slug>/memory/` and create it — `<config>` is `$CLAUDE_CONFIG_DIR` or `~/.claude`
  (`%USERPROFILE%\.claude` on Windows), `<slug>` is the absolute project path with every character
  that is not `A–Z a–z 0–9` replaced by `-`. That is exactly where the resume hook looks; a note
  anywhere else is written but never loaded back.
- **Rolling resume note** — overwritten every time (no dates in the name, no accumulating copies):
  - **One line of work (most projects):** `session_resume_current.md`.
  - **Several long-lived parallel lines in one project** (e.g. a *billing-API* line and a *docs* line
    running for days): one file **per stream**, `session_resume_<stream>.md`. Each session touches
    ONLY its own stream file, so concurrent sessions never clobber each other.
  - Name streams by the **line of work** (stable across sessions), never by session id — a session-id
    name orphans on every new session.
  If the memory directory keeps a `MEMORY.md` index, give the note one stable pointer line there.

## Steps

### 1. Take stock (think before writing)
Scan the conversation for what is **NOT already on disk**. Skip what the repo/git/CLAUDE.md already
records. You are capturing the *delta* since the last checkpoint, not re-summarising the project.

**"Finished" is not a reason to drop something.** The most-lost knowledge is the *working
incantation* — the command, flags, port, profile path or click-path that took more than one attempt
to get right and then quietly worked. It reads like a finished sub-task, so it gets dropped. Sweep
the transcript for these and keep them **verbatim**:
- how a browser/tool was launched (`--remote-debugging-port`, `--user-data-dir`, headed vs headless,
  which profile carries the logged-in session)
- how a server/service was started, on which port, with which env vars
- how auth was obtained and where it lives (path to a token/cookie/profile — **never the secret**)
- the order of steps, when order mattered
- the one flag combination that finally worked after several that did not

### 2. Durable facts → memory files (write, then report)
For each durable, reusable fact from this session (a decision, a constraint, a gotcha, a reference,
a workflow rule):
- **Reusable next week, on any machine** → a **memory file** (launch flags, the port a tool always
  uses, an auth workaround, a tool's real behaviour).
- **True only while these processes / tabs / files are alive** → the **resume note**
  ("Chrome on :9222 is logged into X", "dev server on :5173 since 14:00").
- Unsure → put it in **both**. A duplicated command costs nothing; a lost one costs the session.

Update an existing memory file that already covers the fact instead of duplicating it; follow the
memory conventions already used in that directory (frontmatter shape, index file). Memory writes are
low-risk and reversible → **just do them**, then list them in the report. Delete memories that turned
out wrong.

### 3. Session state → rolling resume note (overwrite the stream's file)
**Pick the target file first:**
- An argument naming a stream (e.g. `/savebeforecompact email`) → `session_resume_<stream>.md`.
- Else one line of work → `session_resume_current.md`. **But read the existing file first:** if it is
  about a *different* line of work than this session, do not overwrite it — write
  `session_resume_<stream>.md` named after this session's work, leave the other note alone, and say
  so in the report. The hook loads the note *this session* wrote, so this session still resumes from
  its own file.
- Else (several streams, no argument) → infer the stream from this session's dominant work and **name
  the file you wrote in the report** so a wrong guess is visible; if genuinely unsure, ask.

**Overwrite only that file — never another stream's.** Frontmatter: copy the shape the directory's
existing memory files use; if there are none, use `name:`, `description:` and `type: project`.

**Run the Step 4 probe before writing** — section 6 below needs its output.

Body, in this order:

1. **ON RESUME — DO THIS FIRST**: the single most likely next action, and which files to read. If the
   user passed an argument, lead with that focus.
2. **Open threads** — every in-flight task as its own bullet: status + the *exact* next step (command
   to run, file to edit, decision pending).
3. **Latest user instructions — verbatim**: the most recent directives still in force, quoted, so
   intent is not lost to paraphrase. Include any "always / never" constraints.
4. **Deliverable status**: files/drafts created this session, each with path + state (done / awaiting
   review / draft-not-sent / needs rework).
5. **Working setup — verbatim**: every command that made something work, **copied from the
   transcript, never retyped from memory**, in a fenced block, one line above each saying what it is
   for. Include flags, ports, profile/user-data paths, env vars, working directory, and the order if it
   mattered. A UI click-path counts. **Redact secret values; keep the path to where the secret lives.**
6. **Live environment — alive right now** (from the Step 4 probe), with the time: servers + ports,
   background jobs, browser instances and which profile is logged into what, watchers, venvs. Say
   which of these will be **gone after a reboot**.
7. **Live TODO snapshot**: if a task list exists, dump the in-progress and pending items.
8. **What worked / what didn't** — working method first (so the next session repeats it), then the
   dead ends (so it does not re-hit them).
9. **Temp / probe files** created (path + purpose + safe to delete?).

Keep it scannable — bullets and short lines. Convert relative dates to absolute.

### 4. Probe the live environment (look, don't remember)
You will not recall what is running. **Run it and paste the output.**

**macOS / Linux:**
```bash
lsof -nP -iTCP -sTCP:LISTEN | awk 'NR>1{print $1, $2, $9}' | sort -u        # servers + ports
ps -Ao pid,etime,command | grep -iE "node|python|flask|uvicorn|vite|http\.server|playwright|chrome|ngrok" | grep -v grep
ps -Ao pid=,command= | grep -E '\-\-(remote-debugging-port|user-data-dir|profile-directory)=' | grep -v grep
```

**Windows** — `lsof` and `ps -Ao` do not exist in Git Bash and `tasklist` drops the arguments, so call
PowerShell (works from Git Bash as written):
```bash
powershell -NoProfile -Command "Get-NetTCPConnection -State Listen | Sort-Object LocalPort -Unique | ForEach-Object { '{0} {1} {2}' -f (Get-Process -Id \$_.OwningProcess).ProcessName, \$_.OwningProcess, \$_.LocalPort }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.Name -match 'node|python|uvicorn|chrome|msedge|ngrok' -and \$_.Name -notmatch 'webview' -and \$_.CommandLine -notmatch ' --type=' } | ForEach-Object { '{0}  {1}  {2}' -f \$_.ProcessId, \$_.CreationDate.ToString('yyyy-MM-dd HH:mm'), \$_.CommandLine }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -match '--(remote-debugging-port|user-data-dir|profile-directory)=' -and \$_.Name -notmatch 'webview' -and \$_.CommandLine -notmatch ' --type=' } | ForEach-Object { '{0}  {1}' -f \$_.ProcessId, \$_.CommandLine }"
```
Keep these one line per process: `Format-List` wraps at console width and splits paths mid-word, and
without the `--type=` / `webview` filters a logged-in `chrome.exe --remote-debugging-port=9222` is
buried under renderer children and embedded WebViews. Don't pipe them through `head` — the line you
need can be last.

The third line recovers **"how do I get back into that logged-in browser"** — the single most-lost
item. Record what it prints even if it looks obvious now. It prints whole command lines on purpose:
profile paths often contain spaces (`Application Support`), so a `grep -oE '...=[^ ]+'` would truncate
them into a path that does not exist — worse than nothing, because it looks usable.

### 5. Self-check — the test that catches what's missing
Re-read the note and answer honestly:

> **Could an agent with ZERO context execute step 1 of "ON RESUME" using only this file?**

Walk the first action literally. Each time the answer is *"it would also need to know X"*, write X
down. Repeat until nothing is missing. If the conversation never established X (a schema, an absolute
path), do not guess — write **"unconfirmed: X — find it with `<command or file>`"**.

Then run the redaction check on **every file written this run** — it must print nothing:
```bash
grep -nE 'sk-[A-Za-z0-9]|api[_-]?key *[=:]|Bearer [A-Za-z0-9._-]{12,}|password *[=:]|_live_[A-Za-z0-9]|eyJ[A-Za-z0-9_-]{8,}' <each-file-written>
```
A secret that appeared in the transcript is never copied — record only where it lives.

### 6. CLAUDE.md — review the whole file, not only the new part
**What qualifies:** durable *project-structure* facts only — a new script, a changed convention, a
new dataset, a corrected gotcha, a status line that is now wrong. Session state goes in the resume
note, never in CLAUDE.md. If nothing qualifies, skip this step and say so.

**Which file:** the CLAUDE.md that governs the folder where the work happened (a subproject's own file
before the root one). Never edit the user-level `~/.claude/CLAUDE.md` — it loads in every project;
propose changes to it only.

**Mode:** check `echo "${SAVEBEFORECOMPACT_CLAUDE_MD:-propose}"` with Bash.
- `propose` (default) → print the proposed diff in chat and leave the file untouched. CLAUDE.md is
  often a shared, committed file; changing it is the user's call.
- `edit` → the user has pre-approved edits: apply them directly, following the rules below, and do
  not wait for confirmation.

**Rules (both modes — a proposal follows them too):**
1. **Measure:** `wc -m <file>` — characters, not bytes (`wc -c` counts bytes, which makes Thai, CJK or
   other non-Latin text look 2–3× larger). The budget is **40,000 characters**, where Claude Code starts
   warning that a large CLAUDE.md hurts performance.
2. **Read the whole file** and look for lines that are **now wrong** because of this session (correct
   them where they are), facts **superseded or said twice** (merge), and **history / measurements**
   (dates, figures, how a thing was found — move them to a HISTORY.md or HANDOFF.md next to it and leave
   a one-line pointer).
3. **Size:** a file under budget may grow but must end under 40,000; a file already over budget must
   come out **the same size or smaller** — make room by condensing, not by leaving the new fact out.
4. **Never delete a rule.** Lines that carry a safety constraint, "never", "do not", or a warning
   marker may be reworded or merged, but their meaning must survive. If something must go and it is
   not clearly history, move it to a doc and link it.
5. *(edit mode)* **Back up first:**
   `mkdir -p ~/.claude/backups/claude-md && cp <file> ~/.claude/backups/claude-md/<folder>-$(date +%Y%m%d-%H%M%S).md`
   — then `diff <backup> <file>`, `wc -m` again, and the Step 5 redaction grep on the file.

### 7. Report + hand over to /compact
End with a short report, **in the user's language**:
- memory files written / updated / deleted (names);
- the resume-note path (name the stream file);
- **how many verbatim commands and live-environment items you recorded** — if zero, say so: a
  session that launched a browser, started a server or fought an auth flow and recorded none of it
  means Step 1 or Step 5 was skipped;
- that the redaction grep came back clean;
- CLAUDE.md: which file, size before → after (chars, against 40,000), what changed or what is proposed,
  and the backup path in edit mode — or that nothing qualified.
- Finish with two lines telling the user what to do next, because the agent can do neither itself
  (the model cannot start `/compact`, and after a compact Claude waits for a user message):
  1. type `/compact` now;
  2. when it finishes, type one word such as `continue` — the hook loads the note and work resumes at
     ON RESUME step 1.

  So **ON RESUME step 1 must be something the next session can start without asking**, unless the work
  is genuinely waiting on the user's decision.

## Guardrails
- **Save only — no outward actions.** Writing memory files, the note and (in edit mode) CLAUDE.md is
  allowed. Editing the files that are the *subject* of the work is not.
- **Idempotent**: running it twice refreshes the same files; it never spawns duplicates.
- **Don't over-capture** what code/git/CLAUDE.md already hold — **but a command that took more than one
  attempt is never over-capture**, even though it succeeded.
- **Verbatim means copied, not remembered.** A reconstructed command is a guess and will be wrong in the
  flag that mattered.
- **Redact secrets** from everything written — keep the *path* to where the secret lives, never the
  value. Prove it with the Step 5 grep.

## The resume side (automatic)
The plugin's **SessionStart hook** loads the note back by itself.

- Fires **only** on `source = compact` and `source = resume` — never on `startup` / `clear`, so an old
  note cannot pollute fresh work. Silent when there is no note; never blocks a session start.
- Picks the `session_resume_*.md` **this session wrote** (from the transcript's Write/Edit calls, else
  the note's `originSessionId`), falling back to the most recently modified one. The project is found
  from `cwd` and its parents (up to 6 levels). Other streams are listed, not loaded.
- After a compact where this session never wrote a note and the newest note belongs to another
  session, it loads **nothing** and says how to read the end of the transcript instead.
- **Work done after the note is recovered from the transcript**: the user's messages verbatim (latest
  ~25, each ≤1,500 chars), files written/edited, the last ~30 Bash commands (redacted, ≤300 chars) and
  Claude's last message — everything after the note was written (or after the previous compact, if the
  note is older). Capped at ~14k chars. Compact summaries, skill bodies, subagent turns and
  `<system-reminder>` blocks are excluded. It costs nothing while working and works the same for a
  manual `/compact` and an auto-compact. **So the note does not have to be up to the minute**; it
  carries what the transcript cannot: why, what next, what failed.

The note is injected verbatim with no human filtering, so Step 5's self-check is the only quality gate.

Notes are per machine: each machine's memory directory lives in its own `<config>/projects/`.

## The context watch (automatic)
The **PostToolUse hook** warns twice per compact cycle:
- at **70 %** of the context window → this skill (full save), then continue the task;
- at **88 %** → this skill with `quick`, then continue.

Settings (environment variables, e.g. in the `env` block of `settings.json`):
`CLAUDE_CONTEXT_WINDOW` (default 200000; set 1000000 for a 1M-context model),
`CLAUDE_CONTEXT_FULL_PCT` / `CLAUDE_CONTEXT_QUICK_PCT` (0–1), `SAVEBEFORECOMPACT_CLAUDE_MD`
(`propose` | `edit`). Where auto-compact fires depends on the model and Claude Code version — if it
compacts before the 88 % warning, lower the tiers. The last reading is written to
`context-watch-last-run` in the plugin's data directory.
