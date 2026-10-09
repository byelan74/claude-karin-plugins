# recall

**Search every past Claude Code session — and every project's memory files — from inside Claude Code.**
Local only. No server, no model calls, no telemetry. Standard-library Python + SQLite.

> ภาษาไทยอยู่ด้านล่าง · [Thai below](#ภาษาไทย)

Claude Code already saves each session as a transcript, but you cannot search them, and it
**deletes them after 30 days** (`cleanupPeriodDays`). recall keeps a searchable copy of
the *conversation* and gives Claude a skill to look things up when you say things like
*"did we ever…"*, *"how did we fix that last time?"*, *"what was the command we used?"*.

```
you:    how did we fix the QX-7731 build error last time?
claude: (runs /recall) On 15 Sep, in zephyr-app, the cause was a stale cache —
        `make purge-zephyr-cache`, then rebuild with --no-incremental.
```

## Install

In Claude Code:

```
/plugin marketplace add byelan74/claude-karin-plugins
/plugin install recall@claude-karin-plugins
```

Restart Claude Code. The first search (or the next session start) indexes everything still on
disk — a few seconds for most people, up to a minute for a very large history.

Update later with `/plugin` (or `claude plugin update recall`). Uninstall with
`claude plugin uninstall recall`; the index in `~/.claude/recall-index/` is kept — delete that
folder to remove it.

## Use

Just ask Claude about earlier work; it calls the skill itself. To force it: `/recall <terms>`.

What the skill can do (Claude runs these for you):

| | |
|---|---|
| `search "<terms>"` | best match per session, newest first, with a pointer to the exact spot |
| `sessions --since 2026-10-01` | what you worked on in a date range |
| `show <id> --last` | where a session ended ("what was I doing?") |
| `forget <id>` | remove a session from the index for good |
| `stats` · `verify` | what is indexed · check the index against the transcripts |

Works with Thai and other languages written without spaces (SQLite FTS5 trigram index).

## What is stored — and what is not

| Stored | Never stored |
|---|---|
| what you typed and what Claude answered | tool output (file contents, command output, web pages, query results) |
| names of files touched, commands run (first 200 chars), tool names | thinking blocks, images, system reminders |
| session title, project, timestamps | `/compact` summaries (they contain tool output) |
| your memory files (`~/.claude/projects/*/memory/*.md`) | input that a program fed to `claude -p` (only Claude's replies are kept) |

The index lives in `~/.claude/recall-index/recall.db` with owner-only permissions and never
leaves your machine. It keeps sessions even after Claude Code deletes the original transcript —
that is the point, so treat the file as you would your chat history.

### Secret redaction — what it catches, and what it does not

Before anything is written, values that look like credentials become `[REDACTED]`:

- `sk-…` keys · `api_key=` / `"api_key": "…"` · `password=` / `password: …`
- shell assignments like `MY_SERVICE_TOKEN=…` / `…_KEY=` / `…_SECRET=` (values ≥ 8 chars)
- `secret=` / `token=` / `client_secret=` (values ≥ 12 chars) · `Bearer …`
- `sk_live_`/`pk_live_`/`rk_live_`/`whsec_live_` keys · AWS `AKIA…` · GitHub `ghp_…` · Slack `xox…`
- JWTs (`eyJ….eyJ….…`) · PEM private key blocks

**It does not catch:** a password written in a sentence ("my password is hunter2"), keys with no
recognisable prefix or label, personal data (names, phone numbers, account numbers), internal
hostnames or IP addresses, or anything inside a file name. If you paste sensitive material
into Claude Code, it can be in the index — use `forget` on that session.

## Requirements

- **Claude Code** with plugin support.
- **Python 3.9+** whose SQLite is **3.43 or newer** (needed for the index format). The tool stops
  with a clear message if yours is older.
  - **macOS:** the built-in `python3` works (tested on macOS 26 with Python 3.9 and 3.14).
  - **Linux:** most current distributions are fine; check with
    `python3 -c "import sqlite3; print(sqlite3.sqlite_version)"`.
  - **Windows:** tested on Windows 11 with Python 3.12 from python.org (SQLite 3.49) under
    Claude Code's Git Bash — indexing, Thai/emoji output, plugin install, the session-start hook
    and Claude calling the skill all work. Use `python` (on Windows `python3` is usually the
    Microsoft Store stub; the hook falls back to `python` by itself).

## Optional: search by meaning

Keyword search is the default. If you run a local embedding server with an OpenAI-compatible
`/v1/embeddings` endpoint (e.g. LM Studio with `bge-m3`), set:

```bash
export RECALL_EMBED_URL=http://127.0.0.1:1234/v1/embeddings
export RECALL_EMBED_MODEL=text-embedding-bge-m3      # default
```

Search then adds a separate "close in meaning" section for hits that use different words.
Keep it pointed at a local server — otherwise your conversation text is sent to it.

## Configuration

All optional environment variables:

| | |
|---|---|
| `RECALL_HOME` | index location (default `~/.claude/recall-index`) |
| `RECALL_EXTRA_PROJECTS` | extra transcript folders, separated by `:` (`;` on Windows). `$CLAUDE_CONFIG_DIR/projects` is included automatically. |
| `RECALL_WORKSPACE` | a folder whose sub-folders are separate projects — nested sessions are grouped under the top-level folder |
| `RECALL_EMBED_URL` / `RECALL_EMBED_MODEL` | see above |

## How it works

- `extract.py` reads `~/.claude/projects/**/*.jsonl` incrementally (byte offsets; a half-written
  last line is never skipped) into SQLite with FTS5. It runs before each search and, through a
  silent background `SessionStart` hook, whenever a session opens.
- `recall.py` ranks matches by recency, collapses them to one result per session, and relaxes
  the query (drop the longest term, then any term) when nothing matches every term.
- Memory files are mirrored on every run, so a memory you delete disappears from search too.

## Development

```bash
python3 tests/test_recall.py        # (from the repo root) 147 tests on synthetic fixtures — never reads your ~/.claude
python3 tools/check_private.py      # also runs as the pre-commit hook (git config core.hooksPath .githooks)
claude plugin validate ./plugins/recall
```

## License

MIT — see [LICENSE](../../LICENSE).

---

## ภาษาไทย

**ค้นบทสนทนาเก่าของ Claude Code ทุกเซสชัน และ memory ของทุกโปรเจกต์ จากใน Claude Code เลย**
ทำงานในเครื่องอย่างเดียว ไม่มี server ไม่เรียกโมเดล ไม่ส่งข้อมูลออก

Claude Code เก็บบทสนทนาไว้ก็จริง แต่ค้นไม่ได้ และ**ลบทิ้งเมื่อครบ 30 วัน** plugin นี้เก็บสำเนา
เฉพาะบทสนทนาไว้ให้ค้น แล้วให้ Claude เรียกใช้เองเมื่อถามว่า *"เคยทำ…ไหม"*, *"ครั้งก่อนแก้ยังไง"*,
*"คำสั่งที่ใช้ตอนนั้นคืออะไร"* — หรือสั่งตรงด้วย `/recall <คำค้น>` · ค้นภาษาไทยได้ (ไม่ต้องเว้นวรรค)

**ติดตั้ง** (พิมพ์ใน Claude Code แล้วเปิดใหม่):
```
/plugin marketplace add byelan74/claude-karin-plugins
/plugin install recall@claude-karin-plugins
```

**เก็บอะไร:** ข้อความที่พิมพ์ · คำตอบของ Claude · *ชื่อ* ไฟล์และคำสั่งที่ใช้ · memory files
**ไม่เก็บ:** ผลลัพธ์ของ tool (เนื้อไฟล์, output คำสั่ง, ผล query) · thinking · รูป · สรุปของ /compact

**ตัวกรอง secret** แทนค่าที่หน้าตาเหมือน key/password/token ด้วย `[REDACTED]` (รายการเต็มด้านบน)
แต่**จับไม่ได้**: รหัสผ่านที่เขียนเป็นประโยค, key ที่ไม่มีรูปแบบที่รู้จัก, ข้อมูลส่วนบุคคล, ชื่อเครื่อง/IP ภายใน —
ถ้าเผลอวางข้อมูลสำคัญลงไป ใช้ `forget <session id>` ลบเซสชันนั้นออก

**ต้องมี:** Python 3.9+ ที่ใช้ SQLite 3.43 ขึ้นไป — macOS ใช้ `python3` ที่มากับเครื่องได้เลย ·
Windows ทดสอบแล้วบน Windows 11 + Python 3.12 จาก python.org ใช้งานได้ครบ (ใช้คำสั่ง `python`)

index อยู่ที่ `~/.claude/recall-index/` เปิดได้เฉพาะเจ้าของเครื่อง · ถอนการติดตั้งแล้ว index ยังอยู่ ลบโฟลเดอร์นั้นเองถ้าต้องการ
