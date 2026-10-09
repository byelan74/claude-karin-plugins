# savebeforecompact

**Keep one long Claude Code session going across many compacts — manual or automatic — without losing the thread.**
Local only. Standard-library Python. No server, no model calls.

> ภาษาไทยอยู่ด้านล่าง · [Thai below](#ภาษาไทย)
> **Status: 0.1.0 — under test.** Works on macOS; Windows testing in progress.

When a session's context fills up, Claude Code compacts it: the conversation is replaced by a
summary. The summary keeps the gist but drops what you need to carry on — the exact command that
finally worked, the port, the profile path, the instruction you gave ten minutes ago, the dead
ends already tried. On a long agentic task, auto-compact can hit in the middle of the work with
nothing saved.

This plugin closes that gap with three pieces:

| Piece | When | What it does |
|---|---|---|
| `/savebeforecompact` skill | you run it, or a hook asks Claude to | writes a **resume note**: the next step, open threads, your latest instructions verbatim, the commands that worked, what is running right now, what failed |
| context-watch hook (`PostToolUse`) | after every tool call | at **70 %** of the context window asks Claude for a full save, at **88 %** for a 30-second `quick` save — then Claude carries on with the task |
| resume hook (`SessionStart`) | after `/compact` or `--resume` | loads the note back **and appends everything that happened after it**, read straight from the transcript: your messages verbatim, files written, commands run, Claude's last message |

The last part is what makes forced auto-compacts safe: anything after the last save is recovered
from the transcript on disk, so the note only has to carry what a transcript cannot — *why*, *what
next*, *what failed*.

## Install

```
/plugin marketplace add byelan74/claude-karin-plugins
/plugin install savebeforecompact@claude-karin-plugins
```

Restart Claude Code. Then set your context window if it is not 200k (see Configuration).

## Use

- Long task running → nothing to do. At 70 % / 88 % Claude saves on its own and continues.
- Want to compact now → `/savebeforecompact`, then `/compact`, then type `continue`.
- Several parallel lines of work in one project → `/savebeforecompact <stream>` keeps one note
  per stream (`session_resume_<stream>.md`), so sessions never overwrite each other's notes.

Claude cannot run `/compact` itself, and after a compact it waits for your next message — that is
Claude Code's design. Typing one word (`continue`) after a compact is enough; the note is already
loaded.

## Where the note lives

In Claude Code's per-project memory directory:
`~/.claude/projects/<project-path-with-dashes>/memory/session_resume_current.md`
(`%USERPROFILE%\.claude\...` on Windows). It is plain markdown — read or edit it any time. Notes
are per machine.

The hook loads the note **this session** wrote (found from the transcript), so two sessions in the
same folder never pick up each other's work. If a session was compacted without ever saving, the
hook says so and points at the end of the transcript instead of loading someone else's note.

## Configuration

Environment variables — put them in the `env` block of `~/.claude/settings.json`:

| | |
|---|---|
| `CLAUDE_CONTEXT_WINDOW` | your model's context window in tokens. Default `200000`; set `1000000` for a 1M-context model |
| `CLAUDE_CONTEXT_FULL_PCT` / `CLAUDE_CONTEXT_QUICK_PCT` | the two warning tiers, 0–1 (default `0.70` / `0.88`). Where auto-compact fires depends on the model and Claude Code version — if it compacts before the second warning, lower them |
| `SAVEBEFORECOMPACT_CLAUDE_MD` | `propose` (default): the skill prints a CLAUDE.md diff and leaves the file alone. `edit`: it edits CLAUDE.md in place — whole-file review, 40k-character budget, never drops a rule, backup first |

```json
{ "env": { "CLAUDE_CONTEXT_WINDOW": "1000000", "SAVEBEFORECOMPACT_CLAUDE_MD": "propose" } }
```

## Privacy

Everything stays on your machine. The note and memory files are written by Claude; the skill runs
a secret-pattern check on every file it writes and records *where* a credential lives, never the
value. The text the resume hook re-injects (your messages, Bash commands) has the same patterns
replaced with `[REDACTED]` — `sk-…`, `api_key=`, `password=`, `Bearer …`, `*_TOKEN=`/`*_SECRET=`,
`…_live_…`, AWS `AKIA…`, GitHub `ghp_…`, JWTs. A password written in a sentence is not caught.

## Requirements

- Claude Code with plugin support.
- Python 3.9+ on `PATH` as `python3` or `python` (Windows: python.org installer; the hooks fall
  back from the Microsoft Store `python3` stub to `python` by themselves).

## Works well with

[`recall`](../recall/) from the same marketplace — if a session was compacted with no note, the
hook suggests `/recall:recall show <session> --last` to see where it ended.

## Development

```bash
python3 tests/test_savebeforecompact.py     # from the repo root — synthetic fixtures, never reads your ~/.claude
claude plugin validate ./plugins/savebeforecompact
```

---

## ภาษาไทย

**ให้ Claude Code เซสชันเดียวทำงานยาว ๆ ผ่านการ compact กี่รอบก็ได้ ไม่ว่าจะสั่งเองหรือระบบบังคับ — โดยไม่หลุดว่างานถึงไหน**

เวลา context เต็ม Claude Code จะ compact คือแทนบทสนทนาด้วยบทสรุป ซึ่งมักทำของสำคัญหาย เช่น คำสั่งที่ลองหลายรอบกว่าจะได้,
port, path ของ profile, คำสั่งล่าสุดที่เราพิมพ์, ทางที่ลองแล้วไม่เวิร์ก — ยิ่งงาน agentic ยาว ๆ ระบบ compact กลางทางโดยยังไม่ได้ save

plugin นี้มี 3 ส่วน:
- **skill `/savebeforecompact`** เขียนโน้ต resume: ขั้นต่อไป, งานค้าง, คำสั่งล่าสุดของเราแบบคำต่อคำ, คำสั่งที่ใช้ได้จริง, อะไรรันอยู่, อะไรไม่เวิร์ก
- **hook เฝ้า context** ถึง 70 % ให้ Claude save เต็ม, ถึง 88 % ให้ quick save แล้วทำงานต่อเอง
- **hook ตอนกลับมา** หลัง compact/resume โหลดโน้ตกลับมา **พร้อมดึงทุกอย่างที่เกิดหลังโน้ตจาก transcript** (ข้อความเรา, ไฟล์ที่แก้, คำสั่งที่รัน, ข้อความสุดท้ายของ Claude)

**ติดตั้ง** (พิมพ์ใน Claude Code แล้วเปิดใหม่):
```
/plugin marketplace add byelan74/claude-karin-plugins
/plugin install savebeforecompact@claude-karin-plugins
```
ถ้าใช้โมเดล context 1M ให้ตั้ง `CLAUDE_CONTEXT_WINDOW=1000000` ใน `env` ของ `~/.claude/settings.json`

**ใช้งาน:** งานยาว — ไม่ต้องทำอะไร · อยาก compact เอง — `/savebeforecompact` → `/compact` → พิมพ์ `ต่อ`
(Claude สั่ง `/compact` เองไม่ได้ และหลัง compact จะรอข้อความจากเรา — เป็นข้อจำกัดของ Claude Code)

**CLAUDE.md:** ค่าเริ่มต้นแค่ *เสนอ* diff ไม่แก้ไฟล์ · ตั้ง `SAVEBEFORECOMPACT_CLAUDE_MD=edit` ถ้าอยากให้แก้เอง (มี backup, คุมขนาด 40k ตัวอักษร, ไม่ลบกฎ)

ทุกอย่างอยู่ในเครื่อง · ค่าที่หน้าตาเหมือน key/password/token ถูกแทนด้วย `[REDACTED]` ก่อนโหลดกลับ
