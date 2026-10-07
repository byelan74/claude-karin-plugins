#!/usr/bin/env python3
"""Fail if anything in the repo points back at the author's machine, employer or clients.

Runs as a test and as the git pre-commit hook (.githooks/pre-commit). The list is the
author's own private vocabulary — extend it, never loosen it to make a commit pass.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = [
    r"karin", r"\bmfec\b", r"onedrive", r"krungsri", r"(?-i:\bBAY\b)", r"aycap",
    r"xsiam", r"xsoar", r"cortex", r"unit ?42", r"freelance_ai", r"servicexcellence",
    r"\b10\.4\.\d", r"\.co\.th\b", r"@gmail\.com", r"/Users/[a-z]", r"claude-karin",
]
_RE = re.compile("|".join(PATTERNS), re.I)
# The author's public credit (LICENSE, plugin.json author) is the one deliberate exception:
# the name itself is published on purpose; everything else matching the patterns is not.
ALLOW = re.compile(r"Karin Naak-in")
SKIP_DIRS = {".git", "__pycache__"}
SELF = Path(__file__).resolve()


def files():
    try:   # inside a git repo: tracked + staged + untracked-but-not-ignored
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split("\n")
        return [ROOT / f for f in out if f]
    except Exception:
        return [p for p in ROOT.rglob("*")
                if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS)]


def main() -> int:
    hits = []
    for p in files():
        if p.resolve() == SELF or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            m = _RE.search(ALLOW.sub("", line))
            if m:
                hits.append(f"{p.relative_to(ROOT)}:{n}: {m.group(0)!r}  {line.strip()[:90]}")
    if hits:
        print("private references found — remove them before committing:")
        print("\n".join("  " + h for h in hits))
        return 1
    print("check_private: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
