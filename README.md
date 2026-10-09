# claude-karin-plugins

Claude Code plugins for working in long sessions without losing track — local only, standard-library
Python, no servers, no model calls.

> ภาษาไทยอยู่ด้านล่าง · [Thai below](#ภาษาไทย)

| Plugin | What it does | Status |
|---|---|---|
| [**recall**](plugins/recall/) | Search every past Claude Code session — and every project's memory files — from inside Claude Code. Keeps a copy before Claude Code's 30-day cleanup deletes transcripts. | 1.0.0 |
| [**savebeforecompact**](plugins/savebeforecompact/) | Keep one long session going across many compacts: saves a resume note, warns at 70 % / 88 % context, and loads the note back — plus everything that happened after it — when the session is compacted or resumed. | 1.0.0 |

Each plugin installs on its own; they also work well together.

## Install

In Claude Code, add this marketplace once:

```
/plugin marketplace add byelan74/claude-karin-plugins
```

then install the ones you want:

```
/plugin install recall@claude-karin-plugins
/plugin install savebeforecompact@claude-karin-plugins
```

Restart Claude Code. Each plugin's README has its configuration and details.

**Moving from `claude-recall`:** this repository used to be `byelan74/claude-recall`. If you
installed `recall@claude-recall`, run `claude plugin uninstall recall@claude-recall`, then the
two commands above. Your index in `~/.claude/recall-index/` is kept.

## Requirements

Claude Code with plugin support · Python 3.9+ (`python3` or `python` on `PATH`). Both plugins are tested on
macOS and Windows 11 (Git Bash, Python 3.12). recall also needs SQLite 3.43+ (see its README).

## Development

```bash
python3 tests/test_recall.py              # synthetic fixtures — never reads your ~/.claude
python3 tests/test_savebeforecompact.py
python3 tools/check_private.py            # also the pre-commit hook: git config core.hooksPath .githooks
claude plugin validate .
```

## License

MIT — see [LICENSE](LICENSE).

---

## ภาษาไทย

plugin สำหรับ Claude Code ที่ช่วยให้ทำงานเซสชันยาว ๆ ได้โดยไม่หลุด — ทำงานในเครื่องอย่างเดียว ไม่ส่งข้อมูลออก

- **recall** — ค้นบทสนทนาเก่าทุกเซสชันและ memory ของทุกโปรเจกต์ (ค้นภาษาไทยได้) · [รายละเอียด](plugins/recall/)
- **savebeforecompact** — ให้เซสชันเดียวทำงานยาวผ่านการ compact หลายรอบ: เขียนโน้ต resume, เตือนที่ 70 %/88 %, โหลดโน้ตพร้อมงานที่เกิดหลังโน้ตกลับมาเองหลัง compact · [รายละเอียด](plugins/savebeforecompact/)

**ติดตั้ง** (พิมพ์ใน Claude Code):
```
/plugin marketplace add byelan74/claude-karin-plugins
/plugin install recall@claude-karin-plugins
/plugin install savebeforecompact@claude-karin-plugins
```
แล้วเปิด Claude Code ใหม่ · เลือกลงตัวเดียวก็ได้
