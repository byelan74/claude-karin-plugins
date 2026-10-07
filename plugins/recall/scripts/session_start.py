#!/usr/bin/env python3
"""SessionStart hook: refresh the recall index in the background, print nothing.

Why at session start: Claude Code deletes transcripts after `cleanupPeriodDays` (30 by
default). Indexing whenever a session opens keeps a copy of every conversation as long as
you use Claude Code at least once in that window — no cron/launchd needed.

Detached so a large first-time index never delays the session, and silent because
anything a SessionStart hook prints becomes context for the model.
"""
import os
import subprocess
import sys
from pathlib import Path

EXTRACT = Path(__file__).resolve().parent / "extract.py"


def main() -> int:
    try:
        sys.stdin.read()                       # hook input is not needed
    except Exception:
        pass
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, str(EXTRACT), "--quiet", "--embed-cap", "0"], **kw)
    except Exception:
        pass                                   # never block or break a session start
    return 0


if __name__ == "__main__":
    sys.exit(main())
