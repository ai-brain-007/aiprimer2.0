#!/usr/bin/env python3
"""The reference-page gate (rule 11 of CLAUDE.md). Keeps the owner's two Notion pages, "Command guide" and
"How the pipeline works" (docs/notion/*.md), in step with the pipeline.

It looks at the commits a `git push` would send:
- they change the pipeline (pipeline/, .claude/skills/, config/) but no file under docs/notion/, and no commit
  message carries the line `Guide: unchanged`   -> fail (exit 2) and say what to do;
- they change docs/notion/                        -> republish the pages first (`python -m pipeline doc guide`);
                                                     a failed publish fails the gate.
Anything else passes silently (exit 0).

Two ways to run it:
- `python3 scripts/hooks/guide_gate.py --check`   by hand, before every push (what the agent does);
- as a Claude Code PreToolUse hook on Bash (the owner registers it in .claude/settings.json): it then reads the
  hook's JSON on stdin and acts only when the command is a `git push`.
Prints no secret, ever.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

PIPELINE_PATHS = ("pipeline/", ".claude/skills/", "config/")
GUIDE_DIR = "docs/notion/"
UNCHANGED = re.compile(r"^\s*guide:\s*unchanged\b", re.IGNORECASE | re.MULTILINE)
PUSH = re.compile(r"\bgit\b[^\n;&|]*\bpush\b")
PUBLISH_TIMEOUT = 270

BLOCK = """guide gate: this push changes the pipeline but not the owner's reference pages.
  changed: {changed}
Before pushing, do one of these:
  1. Update docs/notion/10-command-guide.md and/or docs/notion/20-how-it-works.md so they describe the current
     commands and process, commit, and run the gate again. It republishes them to Notion before the push.
  2. If nothing the owner sees has changed, put the line `Guide: unchanged` in a commit message of this push
     (an extra commit, or amend your own commit) and run the gate again.
This is rule 11 of CLAUDE.md.
"""


def git(root: str | None, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def push_range(root: str) -> str | None:
    """`<base>..HEAD` for the commits a push would send: the upstream, else the remote branch of the same
    name, else the remote default branch. None when nothing can be compared."""
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for ref in ("@{u}", f"origin/{branch}", "origin/HEAD", "origin/main", "origin/master"):
        if git(root, "rev-parse", "--verify", "--quiet", ref).returncode == 0:
            base = git(root, "merge-base", ref, "HEAD")
            if base.returncode == 0 and base.stdout.strip():
                return f"{base.stdout.strip()}..HEAD"
    if git(root, "rev-parse", "--verify", "--quiet", "HEAD~1").returncode == 0:
        return "HEAD~1..HEAD"
    return None


def publish(root: str) -> tuple[bool, str]:
    cmd = shlex.split(os.environ.get("AIPRIMER_GUIDE_PUBLISH_CMD") or "") or [sys.executable, "-m", "pipeline", "doc", "guide"]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=PUBLISH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    last = (proc.stdout.strip().splitlines() or [""])[-1]
    try:
        result = json.loads(last)
    except json.JSONDecodeError:
        return False, (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}, no JSON on stdout"])[-1]
    if not result.get("ok"):
        return False, str(result.get("error") or "unknown error")
    pages = result.get("pages") or []
    return True, ", ".join(f"{p.get('title')} ({'created' if p.get('created') else 'refreshed'})" for p in pages) or "no page"


def check(root: str) -> int:
    rng = push_range(root)
    if not rng:
        return 0
    files = git(root, "diff", "--name-only", rng).stdout.split()
    messages = git(root, "log", "--format=%B", rng).stdout
    pipeline_changed = sorted(f for f in files if f.startswith(PIPELINE_PATHS))
    guides_changed = sorted(f for f in files if f.startswith(GUIDE_DIR))
    if pipeline_changed and not guides_changed and not UNCHANGED.search(messages):
        sys.stderr.write(BLOCK.format(changed=", ".join(pipeline_changed[:8]) + (" ..." if len(pipeline_changed) > 8 else "")))
        return 2
    if guides_changed:
        if not os.environ.get("AIPRIMER_NOTION_PAGE_ID"):
            sys.stderr.write("guide gate: docs/notion changed but Notion mode is off here; run `python -m pipeline doc guide` from the AI Primer 2.0 environment.\n")
            return 0
        ok, detail = publish(root)
        if not ok:
            sys.stderr.write(f"guide gate: docs/notion changed but republishing the reference pages failed, so the push is held: {detail}\nFix it (or run `python -m pipeline doc guide --pretty` to see the error) and run the gate again.\n")
            return 2
        sys.stderr.write(f"guide gate: reference pages republished to Notion: {detail}\n")
    else:
        sys.stderr.write("guide gate: ok, nothing the owner sees has changed.\n" if pipeline_changed else "guide gate: ok.\n")
    return 0


def main(argv: list[str]) -> int:
    if "--check" not in argv:  # hook mode: act only on a `git push` issued through the Bash tool
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, OSError):
            return 0
        if payload.get("tool_name") != "Bash":
            return 0
        if not PUSH.search(str((payload.get("tool_input") or {}).get("command") or "")):
            return 0
    top = git(None, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        sys.stderr.write("guide gate: not inside a git repository.\n")
        return 0 if "--check" not in argv else 2
    return check(top.stdout.strip())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
