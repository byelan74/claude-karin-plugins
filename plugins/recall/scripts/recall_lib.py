"""Shared pieces for claude-recall — a local, searchable index of your Claude Code sessions.

WHY THIS EXISTS
  Claude Code already writes every session to ~/.claude/projects/<slug>/<uuid>.jsonl.
  Capture is solved; what is missing is SEARCH. This package indexes only the conversation
  (what you typed, what Claude replied) plus the *names* of files and commands that were
  touched — never tool output. On the author's machine that was ~1 % of the raw transcripts.

  The index keeps its own copy of the text on purpose: `cleanupPeriodDays` defaults to 30,
  so the .jsonl files are deleted out from under us. A pointer-only index would rot.

WHAT NEVER ENTERS THE DATABASE
  tool_result / tool_use payloads, thinking blocks, images, injected <system-reminder>
  blocks, and any secret VALUE the redactors recognise. The marker survives on purpose
  (`api_key=[REDACTED]`): that a key was discussed is worth remembering, its value is not.
  LEAK_PATTERNS below is the test-suite oracle for "is a value still exposed".

CONFIGURATION — environment variables, all optional
  RECALL_HOME            where the index lives (default ~/.claude/recall-index)
  RECALL_EXTRA_PROJECTS  extra transcript roots, separated by os.pathsep
                         ($CLAUDE_CONFIG_DIR/projects is added automatically when set)
  RECALL_WORKSPACE       a folder whose top-level sub-folders are your projects; sessions in
                         nested folders are then grouped under the top-level one
  RECALL_EMBED_URL       OpenAI-compatible /v1/embeddings endpoint (e.g. LM Studio on
                         127.0.0.1) — turns on the optional "close in meaning" search
  RECALL_EMBED_MODEL     embedding model name (default text-embedding-bge-m3)
  RECALL_PROJECTS_DIR / RECALL_DB   test-only: one throwaway root / index file
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The machine's local timezone, so dates read the way you lived them (an evening session
# would otherwise show up under the next UTC day).
LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc

HOME = Path.home()
RECALL_DIR = Path(__file__).resolve().parent          # the code — replaced on plugin update
DATA_DIR = Path(os.environ.get("RECALL_HOME", HOME / ".claude" / "recall-index")).expanduser()
SCHEMA_VERSION = "1"

# contentless_delete arrived in SQLite 3.43.0 and the trigram tokenizer in 3.34.0.
MIN_SQLITE = (3, 43, 0)


def _projects_dirs():
    if "RECALL_PROJECTS_DIR" in os.environ:            # tests: a single throwaway root
        return [Path(os.environ["RECALL_PROJECTS_DIR"])]
    roots = [HOME / ".claude" / "projects"]
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        roots.append(Path(cfg).expanduser() / "projects")
    for extra in os.environ.get("RECALL_EXTRA_PROJECTS", "").split(os.pathsep):
        if extra.strip():
            roots.append(Path(extra.strip()).expanduser())
    out = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


PROJECTS_DIRS = _projects_dirs()
PROJECTS_DIR = PROJECTS_DIRS[0]
DB_PATH = Path(os.environ.get("RECALL_DB", DATA_DIR / "recall.db")).expanduser()

_ws = os.environ.get("RECALL_WORKSPACE", "").strip()
WORKSPACE = Path(_ws).expanduser().resolve() if _ws else None
WORKSPACE_SLUG = re.sub(r"[^A-Za-z0-9]", "-", str(WORKSPACE)) if WORKSPACE else None

# ---------------------------------------------------------------- redaction

# The "does this line look like it carries a secret" markers — a review signal for humans.
# The enforcement oracle is LEAK_PATTERNS further down.
DETECTOR_PATTERNS = [
    r"sk-[A-Za-z0-9]",
    r"api[_-]?key *[=:]",
    r"Bearer ",
    r"password *[=:]",
    r"_live_[A-Za-z0-9]",
]

# Redactors. Every one of these shapes was either found leaking in a real archive or
# found CORRUPTING it. Both failures matter: rewriting `task-manager` to `ta[REDACTED]`
# to hide a secret that was never there is worse than not redacting.
#
#   * `sk-` must not match inside a word (`task-manager` contains `sk-m`).
#   * `Bearer` excludes the literal next word "token": the prose "Bearer token" is common
#     and was being rewritten to "Bearer [REDACTED]".
#   * separators use [ \t]* AFTER the =/:, never \s*, which crossed newlines and ate the
#     line following a bare `Password:` sudo prompt.
#   * `"api_key": "value"` — the JSON form — needs the optional quote; the earlier
#     pattern required =/: immediately after the key name and missed every config file.
#   * `_live_` is limited to known key prefixes. `[A-Za-z]+_live_` swallowed `go_live_date`
#     and `is_live_now`, which on a go-live project is a lot of real text.
#   * no length floor on the key shapes: `pk_live_abc` must still be caught.
_SEP = r"""["']?[ \t]*[=:][ \t]*["']?"""
_REDACTORS = [
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]+"), "[REDACTED]"),
    (re.compile(r"(api[_-]?key" + _SEP + r")([^\s\"']+)", re.I), r"\1[REDACTED]"),
    (re.compile(r"(password" + _SEP + r")([^\s\"']+)", re.I), r"\1[REDACTED]"),
    # shell env assignment: `export SOME_TENANT_KEY='…'` leaked a real key into touches
    # during review. Values starting with / ~ $ are paths or references, and < 8 chars are
    # settings (MAX_TOKEN=4096), not secrets.
    (re.compile(r"(\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)=[\"']?)"
                r"(?![/~$])([^\s\"']{8,})"), r"\1[REDACTED]"),
    (re.compile(r"((?:client_secret|secret|token)" + _SEP + r")([^\s\"']{12,})", re.I),
     r"\1[REDACTED]"),
    (re.compile(r"(Bearer[ \t]+)(?![Tt]oken\b)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?<![A-Za-z0-9])(?:sk|pk|rk|whsec|stripe)_live_[A-Za-z0-9]+"),
     "[REDACTED]"),
    # provider-specific shapes, added after an AWS access key id was found in the archive
    (re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+"),
     "[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.S), "[REDACTED PRIVATE KEY]"),
]

# Machine-injected blocks. Not conversation: they repeat thousands of times, bloat a
# trigram index, and make every search match every session. Stripped with str.find rather
# than a regex — `<tag>.*?</tag>` over text containing many unclosed openers backtracks
# quadratically (measured: 18.7 s on a crafted 100 KB message).
_TAG_PAIRS = [
    ("<system-reminder>", "</system-reminder>"),
    ("<local-command-stdout>", "</local-command-stdout>"),
    ("<local-command-caveat>", "</local-command-caveat>"),
    ("<persisted-output>", "</persisted-output>"),
    ("<task-notification>", "</task-notification>"),
    ("<command-name>", "</command-name>"),
    ("<command-message>", "</command-message>"),
    ("<command-args>", "</command-args>"),
]

_STRIP_LINES = [
    re.compile(r"^Caveat: The messages below.*?$", re.M),
]

_WS = re.compile(r"[ \t]+")
_NL = re.compile(r"\n{3,}")


def _strip_pairs(text: str) -> str:
    for open_tag, close_tag in _TAG_PAIRS:
        while True:
            i = text.find(open_tag)
            if i < 0:
                break
            j = text.find(close_tag, i + len(open_tag))
            if j < 0:
                break  # unclosed: leave it alone rather than eat the rest of the message
            text = text[:i] + " " + text[j + len(close_tag):]
    return text


def redact(text: str) -> str:
    for pat, repl in _REDACTORS:
        text = pat.sub(repl, text)
    return text


def clean_text(text: str) -> str:
    """Strip injected blocks, collapse whitespace, redact secrets."""
    if not text:
        return ""
    text = _strip_pairs(text)
    for pat in _STRIP_LINES:
        text = pat.sub(" ", text)
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return redact(text.strip())


# The oracle for "did a secret get through". It is NOT the same thing as DETECTOR_PATTERNS
# and the difference is the whole point:
#
#   DETECTOR_PATTERNS flag a LINE THAT LOOKS LIKE it carries a secret, for a human to
#   eyeball. `api_key=[REDACTED]` still trips it — correctly, as a review signal.
#
#   Here we need "is a secret VALUE exposed", so each pattern is the same marker plus a
#   negative lookahead for the replacement. The marker is deliberately left in the text:
#   knowing an API key was discussed is useful; knowing its value is not.
#
# This is a narrower question, not a looser detector: every pattern below still starts from
# the same marker set, and none of them has a length floor (`pk_live_abc`, three characters
# of value, must still be caught).
# (pattern, flags) — flags mirror the redactor above, for the reasons given there.
LEAK_PATTERNS = [
    (r"(?<![A-Za-z0-9])sk-(?!\[REDACTED\])[A-Za-z0-9]", 0),
    (r"api[_-]?key" + _SEP + r"(?!\[REDACTED\])[^\s\"']", re.I),
    (r"password" + _SEP + r"(?!\[REDACTED\])[^\s\"']", re.I),
    (r"\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)=[\"']?(?![/~$])(?!\[REDACTED\])"
     r"[^\s\"']{8,}", 0),
    (r"(?:client_secret|secret|token)" + _SEP + r"(?!\[REDACTED\])[^\s\"']{12,}", re.I),
    (r"Bearer[ \t]+(?![Tt]oken\b)(?!\[REDACTED\])\S", 0),
    (r"(?<![A-Za-z0-9])(?:sk|pk|rk|whsec|stripe)_live_(?!\[REDACTED\])[A-Za-z0-9]", 0),
    (r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}", 0),
    (r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{20,}", 0),
    (r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9\-]{10,}", 0),
    (r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+", 0),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", 0),
]
_LEAK_RE = [re.compile(p, f) for p, f in LEAK_PATTERNS]


def blocks_text(content) -> str:
    """Conversation text only: a plain string, or the `text` blocks of a content list.

    Verified against real records: tool_result keeps its payload under `content`, not
    `text`, so selecting type == 'text' cannot leak tool output. Lives here rather than in
    extract.py so `verify` can re-run the identical extraction and compare round-trip.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content
                          if isinstance(b, dict) and b.get("type") == "text")
    return ""


def detector_hits(text: str):
    """Secret VALUES still exposed in `text`. Empty list == clean. See LEAK_PATTERNS."""
    return [p.pattern for p in _LEAK_RE if p.search(text)]


def marker_hits(text: str):
    """Raw marker grep — matches even after redaction. Review signal only."""
    return [p for p in DETECTOR_PATTERNS if re.search(p, text)]


# ---------------------------------------------------------------- projects

_SCRATCH_RE = re.compile(r"^/(?:private/)?tmp/claude-\d+/([^/]+)/")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", s)


# slug -> project name, learned from every real cwd seen (and seeded by extract.py from the
# index). Claude Code's slug is lossy — every separator becomes '-' — so a slug alone cannot
# be turned back into a folder name; a cwd we have already seen can.
KNOWN_SLUGS: dict = {}

_DIRMAP_CACHE = DATA_DIR / "dirmap.json"


def _scan_dir_names():
    out = {}
    for entry in os.scandir(WORKSPACE):
        if entry.is_dir() and not entry.name.startswith("."):
            out[_slug(entry.name)] = entry.name
    return out


_dir_by_slug = None


def dir_by_slug():
    """RECALL_WORKSPACE only: slugified top-level folder name -> real folder name.

    LAZY, with a timeout, and cached to disk: a workspace inside a cloud-synced folder can
    hang a directory scan indefinitely when no GUI session is running (a nightly job sat at
    state=running with an empty log). The same scan takes 0.00 s from a foreground shell,
    which is exactly why it looked fine in testing.
    """
    global _dir_by_slug
    if _dir_by_slug is not None:
        return _dir_by_slug
    result = {}
    if WORKSPACE is None:
        _dir_by_slug = result
        return result
    try:
        # A daemon thread, not `with ThreadPoolExecutor`: the executor's __exit__ waits for
        # the hung scan to finish, so a "5 s timeout" would really wait as long as the hang.
        import threading
        box: dict = {}
        th = threading.Thread(target=lambda: box.update(_scan_dir_names()), daemon=True)
        th.start()
        th.join(5)
        if th.is_alive():
            raise TimeoutError("workspace scan hung")
        result = dict(box)
        try:
            _DIRMAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _DIRMAP_CACHE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    except Exception:
        # timed out or failed: fall back to the last good scan — never a hang, never a crash
        try:
            result = json.loads(_DIRMAP_CACHE.read_text(encoding="utf-8"))
        except Exception:
            result = {}
    _dir_by_slug = result
    return _dir_by_slug


def slug_to_project(slug: str) -> str:
    """A projects-dir slug ('-Users-me-code-my-app') -> a readable project name."""
    # a scratchpad cwd slugs to '-private-tmp-claude-501--Users-…'; drop the tmp prefix so
    # work done from a scratchpad is filed under its real project
    slug = re.sub(r"^-private-tmp-claude-\d+-", "", slug)
    if slug in KNOWN_SLUGS:
        return KNOWN_SLUGS[slug]
    if WORKSPACE_SLUG and slug.startswith(WORKSPACE_SLUG):
        rest = slug[len(WORKSPACE_SLUG):].lstrip("-")
        if not rest:
            return WORKSPACE.name
        dmap = dir_by_slug()
        if rest in dmap:
            return dmap[rest]
        # nested path: keep the longest known top-level folder as the project
        for known in sorted(dmap, key=len, reverse=True):
            if rest == known or rest.startswith(known + "-"):
                return dmap[known]
        return rest
    if slug == _slug(str(HOME)):
        return "~"
    return slug.lstrip("-") or "?"


def project_for(cwd: str, jsonl_path: str) -> str:
    """Human-readable project name. Scratchpad dirs map back to their parent project."""
    if cwd:
        m = _SCRATCH_RE.match(cwd)
        if m:
            return slug_to_project(m.group(1))
        try:
            p = Path(cwd)
            if WORKSPACE is not None:
                if p == WORKSPACE:
                    name = WORKSPACE.name
                elif WORKSPACE in p.parents:
                    name = p.relative_to(WORKSPACE).parts[0]
                else:
                    name = None
                if name:
                    KNOWN_SLUGS[_slug(cwd)] = name
                    return name
            name = "~" if p == HOME else (p.name or str(p))
            KNOWN_SLUGS[_slug(cwd)] = name
            return name
        except (ValueError, OSError):
            pass
    # No cwd on any record: fall back to the directory name, which is the slugified cwd.
    return slug_to_project(Path(jsonl_path).parent.name)


# ---------------------------------------------------------------- database

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);

-- One row per (session, source file). A session spans two files when it was --resumed,
-- so counts are aggregated from here into `sessions` after the scan, never during it.
CREATE TABLE IF NOT EXISTS session_files(
  session_id TEXT, src_path TEXT,
  cwd TEXT, project TEXT, git_branch TEXT,
  title TEXT, first_user_msg TEXT,
  started_at TEXT, ended_at TEXT, entrypoint TEXT,
  PRIMARY KEY(session_id, src_path));

-- Derived. Rebuilt from session_files + turns at the end of every scan.
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY,
  jsonl_path TEXT, jsonl_exists INT,
  project TEXT, cwd TEXT, git_branch TEXT,
  title TEXT, first_user_msg TEXT,
  started_at TEXT, ended_at TEXT,
  n_user INT, n_assistant INT, bytes_text INT,
  indexed_at TEXT, entrypoint TEXT);

-- Source paths are ~120 characters and repeat on every row. Storing an integer instead
-- measured 15.2 MB smaller across turns + touches + their indexes.
CREATE TABLE IF NOT EXISTS files(
  file_id INTEGER PRIMARY KEY, path TEXT UNIQUE);

CREATE TABLE IF NOT EXISTS turns(
  turn_id INTEGER PRIMARY KEY,
  session_id TEXT, file_id INTEGER, seq INTEGER,
  role TEXT, is_sidechain INT,
  ts TEXT, line_no INTEGER,
  text TEXT);

-- Names only: file paths, command lines (truncated + redacted), tool names. No payloads.
-- Deduplicated per (session, file, kind, value) with a count: a session that ran Bash 400
-- times should be one row saying 400, not 400 rows saying Bash.
CREATE TABLE IF NOT EXISTS touches(
  touch_id INTEGER PRIMARY KEY,
  session_id TEXT, file_id INTEGER, kind TEXT, value TEXT,
  n INTEGER DEFAULT 1, first_ts TEXT, last_ts TEXT,
  UNIQUE(session_id, file_id, kind, value));

CREATE TABLE IF NOT EXISTS files_seen(
  path TEXT PRIMARY KEY, size INTEGER, mtime REAL,
  offset INTEGER, line_no INTEGER, last_scan TEXT);

CREATE INDEX IF NOT EXISTS ix_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS ix_turns_file    ON turns(file_id);
CREATE INDEX IF NOT EXISTS ix_touch_session ON touches(session_id);
CREATE INDEX IF NOT EXISTS ix_touch_file    ON touches(file_id);
CREATE INDEX IF NOT EXISTS ix_sess_project  ON sessions(project);
CREATE INDEX IF NOT EXISTS ix_sess_ended    ON sessions(ended_at);

-- Contentless FTS5 (`content=''`) with contentless_delete=1 (SQLite >= 3.43). Three reasons for this shape rather than the two obvious ones:
--   * contentless stores no second copy of the text -> 24 MB smaller than a plain table
--   * contentless_delete makes `DELETE ... WHERE rowid=?` work, which `forget` and a file
--     re-scan both need. External-content tables need the OLD value fed back on delete and
--     corrupt the index in silence when that is missed.
--   * snippets are built from turns.text by rowid instead of snippet(), which costs one
--     lookup and gives control over how much context is shown.
-- trigram is mandatory for Thai: unicode61 splits on whitespace, and Thai has none, so a
-- whole sentence becomes one token and nothing is ever found. Verified both ways.
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts
  USING fts5(text, content='', contentless_delete=1, tokenize='trigram');
-- Paths and commands are ASCII, so unicode61 is enough and much smaller than trigram.
CREATE VIRTUAL TABLE IF NOT EXISTS touches_fts
  USING fts5(value, content='', contentless_delete=1, tokenize='unicode61');

-- Claude Code memory files (<project>/memory/*.md) of EVERY project. The conversation
-- index cannot find them: a memory file is written through a Write tool_use, whose payload
-- is dropped by design, so the distilled lesson only ever existed as a file name. Memory is
-- also loaded per-cwd, so a lesson written in one project is invisible from another.
-- Mirrored in full on every extract (cheap): a file deleted on disk disappears here too.
CREATE TABLE IF NOT EXISTS memories(
  mem_id INTEGER PRIMARY KEY, path TEXT UNIQUE, project TEXT,
  name TEXT, description TEXT, mtype TEXT, body TEXT,
  size INTEGER, mtime REAL, indexed_at TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
  USING fts5(text, content='', contentless_delete=1, tokenize='trigram');

-- Optional embedding vectors (float32, L2-normalised) for "close in meaning" search.
-- Keyed by turn_id: every path that deletes a turn deletes its vector too.
CREATE TABLE IF NOT EXISTS turn_vecs(turn_id INTEGER PRIMARY KEY, vec BLOB);
"""


_OPENED: dict = {}


def connect(path: Path = None, create: bool = True) -> sqlite3.Connection:
    path = Path(path) if path else DB_PATH
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise SystemExit(
            f"no index at {path}\nrun:  python3 {RECALL_DIR}/extract.py --full"
        )
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # A search refreshes the index while a background/nightly extract may be writing.
    conn.execute("PRAGMA busy_timeout=15000")
    # sqlite3.Connection has no __dict__, so the path is remembered on the side. It is
    # needed by ensure_schema, which must chmod the file it actually opened (tests open a
    # temp db via --db / RECALL_DB, and chmodding DB_PATH there is simply wrong).
    _OPENED[id(conn)] = path
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise SystemExit(
            f"claude-recall needs SQLite >= {'.'.join(map(str, MIN_SQLITE))} "
            f"(this Python has {sqlite3.sqlite_version}). Use a newer python3.")
    conn.executescript(SCHEMA)
    # columns added after the first release — CREATE IF NOT EXISTS does not add them
    for table, col in (("session_files", "entrypoint"), ("sessions", "entrypoint"),
                       ("memories", "vec")):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} "
                         f"{'BLOB' if col == 'vec' else 'TEXT'}")
    conn.execute(
        "INSERT INTO meta(k,v) VALUES('schema_version',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (SCHEMA_VERSION,),
    )
    conn.commit()
    # The index holds whatever you typed into Claude Code — owner-only.
    target = _OPENED.get(id(conn), DB_PATH)
    try:
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(target) + suffix)
            if f.exists():
                os.chmod(f, 0o600)
    except OSError:
        pass


def acquire_lock(db_path: Path, blocking: bool):
    """flock on <db>.lock. Returns the open handle (keep it alive) or None if busy.

    No fcntl (Windows): runs unlocked — two simultaneous extracts could then duplicate rows.
    """
    try:
        import fcntl
    except ImportError:
        fcntl = None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(str(db_path) + ".lock", "w")
    if fcntl is None:
        return fh
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except OSError:
        fh.close()
        return None
    return fh


# ---------------------------------------------------------------- embeddings
#
# OFF unless RECALL_EMBED_URL is set. Point it at a LOCAL server (LM Studio, Ollama's
# OpenAI-compatible endpoint, …) — the "nothing leaves this machine" property depends on it.
EMBED_URL = os.environ.get("RECALL_EMBED_URL", "").strip()
EMBED_MODEL = os.environ.get("RECALL_EMBED_MODEL", "text-embedding-bge-m3")
EMBED_ENABLED = bool(EMBED_URL)
EMBED_MIN_CHARS = 40      # "ok", "ต่อเลย" carry no meaning worth a vector
EMBED_HEAD = 2000         # chars embedded per turn; a lesson at the end of a long answer
                          # is not in its vector (chunking would fix it at ~3x the vectors)


def embed(texts, timeout: float):
    """Vectors from the configured endpoint, as array('f'). Raises on any failure."""
    import array
    import urllib.request
    req = urllib.request.Request(
        EMBED_URL, data=json.dumps({"model": EMBED_MODEL, "input": list(texts)}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)["data"]
    return [array.array("f", d["embedding"]) for d in sorted(data, key=lambda d: d["index"])]


def unpack_vec(blob):
    import array
    v = array.array("f")
    v.frombytes(blob)
    return v


def dot(a, b) -> float:
    """Cosine for unit-length vectors (bge-m3 returns them normalised)."""
    import operator
    return sum(map(operator.mul, a, b))


def utf8_stdio() -> None:
    """Windows consoles default to a legacy code page; Thai or emoji output would crash."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def file_id(conn, path: str) -> int:
    """Intern a source path and return its integer id."""
    conn.execute("INSERT OR IGNORE INTO files(path) VALUES(?)", (path,))
    return conn.execute("SELECT file_id FROM files WHERE path=?", (path,)).fetchone()[0]


def transcript_files():
    """Every transcript, main sessions and subagents alike.

    Claude Code writes delegated work to <slug>/<uuid>/subagents/agent-*.jsonl. Large raw,
    tiny as conversation — cheap to keep, and without them `--sidechain` would be a switch
    that never matches anything.
    """
    return sorted(str(p) for p in
                  [p for root in PROJECTS_DIRS for p in
                   list(root.glob("*/*.jsonl")) + list(root.glob("*/*/subagents/*.jsonl"))])


# ---------------------------------------------------------------- format drift
#
# The .jsonl format is not a published contract; everything the extractor relies on
# (`entrypoint`, `isCompactSummary`, `isMeta`, block types) was observed, not documented.
# A Claude Code update that renames one of
# these would not raise anything — it would silently start indexing pipeline payloads or
# /compact summaries. extract records what it sees; `verify` compares.
KNOWN_RECORD_TYPES = {
    "user", "assistant", "system", "attachment", "ai-title", "custom-title", "agent-name",
    "mode", "permission-mode", "file-history-snapshot", "file-history-delta", "last-prompt",
    "queue-operation", "bridge-session", "atis-latch", "cost-state", "frame-link",
    "artifact-comment-monitor", "artifact-autoreact-ledger", "history-suppression",
}
KNOWN_BLOCK_TYPES = {
    "text", "thinking", "tool_use", "tool_result", "image", "document",
    "server_tool_use", "advisor_tool_result", "redacted_thinking",
}
# Canaries that fire whatever the fields are called: if one of these texts is ever indexed,
# an exclusion flag stopped working.
CANARIES = (
    ("compact summary", "role='user' AND text LIKE 'This session is being continued from a previous%'"),
    ("skill body", "role='user' AND text LIKE 'Base directory for this skill:%'"),
    ("pipeline payload", "role='user' AND text LIKE '[System Instructions]%'"),
)


# Not memories, although they live in the same folder:
#   MEMORY.md          the index — one pointer line per file, so every query would hit it
#   session_resume_*   rolling "where was I" notes some setups keep; as search results they
#                      would surface yesterday's to-do as a lesson
_MEMORY_SKIP = re.compile(r"^(MEMORY\.md|session_resume_.*\.md)$")


def memory_files():
    return sorted(str(p) for root in PROJECTS_DIRS for p in root.glob("*/memory/*.md")
                  if not _MEMORY_SKIP.match(p.name))


def parse_memory(text: str, stem: str) -> dict:
    """Frontmatter name/description/type + body. Tolerates files without frontmatter.

    `type` is read at any indent because the files carry it both at the top level and
    nested under `metadata:` (the harness rewrites frontmatter into the nested form).
    """
    name, desc, mtype, body = stem, "", "", text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            fm, body = text[3:end], text[end + 4:].lstrip("\n")
            for line in fm.splitlines():
                m = re.match(r"^(\s*)(name|description|type):\s*(.*)$", line)
                if not m:
                    continue
                key, val = m.group(2), m.group(3).strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1].replace('\\"', '"')
                if key == "name" and not m.group(1) and val:
                    name = val
                elif key == "description" and not m.group(1):
                    desc = val
                elif key == "type" and not mtype:
                    mtype = val
    return {"name": name, "description": desc, "mtype": mtype, "body": body.strip()}


def local_date(iso: str) -> str:
    """UTC ISO -> the local calendar date. Slicing the ISO string would put evening
    sessions on the wrong day — and the skill tells Claude to cite the date."""
    if not iso:
        return "?"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def local_stamp(iso: str, width: int = 16) -> str:
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:width]
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")[:width]
