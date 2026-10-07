---
name: recall
description: Search a local index of ALL past Claude Code sessions and the memory files of EVERY project (the memory loaded right now covers only the current folder). Use when (1) the user refers to earlier work or conversations — "did we ever…", "last time", "previously", "how did we fix…", "what did we decide", "the command we used", "what did I do yesterday / last week", "เคยทำ/เคยคุย…ไหม", "ครั้งก่อน", "ตอนนั้นทำยังไง", "ที่เคยคุยกัน", "เมื่อวานทำอะไรไว้"; (2) the user asks for work on a tool, system or client that this folder's CLAUDE.md/memory does not mention but another project probably did — search once before starting; (3) an error or odd behaviour looks like something solved before. Returns pointers, not bulk text, so it is cheap on context. Can be forced with /recall. Do NOT use for questions answerable by reading the current files, for things still visible in the current context, or in automated pipeline prompts.
argument-hint: "<search terms>"
---

# recall — search your past Claude Code sessions

Claude Code already saves every session as a transcript (`~/.claude/projects/**.jsonl`) and
deletes them after `cleanupPeriodDays` (30 by default). This skill keeps a **local, searchable
copy of the conversation only** — what the user typed, what Claude replied, and the *names* of
files and commands that were touched. It never stores tool output, and secret values it
recognises are replaced with `[REDACTED]`. Nothing leaves the machine.

## Commands

Run with `python3` — **on Windows use `python`** (there `python3` is usually the Microsoft Store stub, which exits with an error):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" search "<terms>"            # search
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" search "<terms>" --json     # easier to parse
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" search "<terms>" --project my_app --since 2026-09-01
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" sessions --since 2026-10-01 # list sessions by date
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" sessions --grep deploy      # match session titles
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" show <sid> --line 7242      # read around a hit
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" show <sid> --last           # where a session ended
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" stats                       # what is in the index
```

`<sid>` accepts a short prefix (e.g. `3f9a1c2e`). The index refreshes itself before every
search, so results include the current session. The first run indexes the whole archive and
may take a minute on a large one.

## How to use it well

1. **Pointers first, then read only what matters.** `search` returns short snippets. Pick the
   relevant hit and `show` just that one. Never `show --all` several sessions in a row — that
   pours the archive back into context, which is what this tool exists to avoid.
2. **Search with the words the user would have typed**, as keywords, not whole sentences.
   Thai and other scripts without spaces work (trigram index), but a long clause is one
   substring that rarely matches. If not every term matches, the tool relaxes the query
   (drops the longest term, then ANY term) and says which strategy it used — a relaxed result
   is looser than what was asked.
3. **Time questions are not keyword questions.** "What did I do yesterday?" has no content
   words — use `sessions --since <date>` instead of `search`.
4. **The 📌 memory section comes first and outranks chat.** Memory files are distilled notes;
   when one answers the question, `Read` the file at the path shown. Use the raw conversation
   for *why* or *what happened*. A memory from another project was written in that project's
   context — check the project name before applying it.
5. **Dates shown are the date of the matching message**, not the session's end — a session
   resumed over weeks shows its span separately.
6. **Cite what you used** ("on 13 Sep in project my_app you said…") so the user can check, and
   **do not present archive content as current fact**: files and settings may have changed
   since. Open the real file before recommending something the archive mentions.
7. Answer in the user's language; the tool's own output is English.

Hidden by default: subagent turns (`--sidechain` to include) and `claude -p` / SDK pipeline
sessions (`--headless` to include — only Claude's replies are stored for those, never the
program-fed input).

## Promote what matters

If a search turns up something that will be needed again — a decision with its reason, a trap
that already cost time, a command that took several tries, a rule the user set — save it as a
memory file in the current project rather than leaving it only in the archive.

## Privacy and forgetting

The index lives in `~/.claude/recall-index/` (owner-only permissions). To remove a session:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" forget <sid> --dry-run   # preview
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" forget <sid>
```

The original transcript is not touched, and a forgotten session is not re-indexed.

## If something looks wrong

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/extract.py" --full    # rebuild (keeps sessions whose .jsonl is gone)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" verify     # check the index against the transcripts
```
