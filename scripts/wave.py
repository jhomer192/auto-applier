#!/usr/bin/env python3
"""wave.py — mark a wave boundary so the next turn starts with clean context.

A wave accumulates a lot of context, almost all of it Playwright page snapshots that
are worthless the moment you leave the page. None of it is load-bearing: every durable
fact lives in data/*.csv (applied, seen, retry, board_rotation) and in the job queue
scripts/source.py hands you. So at the end of a wave — after you've posted the summary
line — call:

    python3 scripts/wave.py end

That drops data/session_reset.flag. The Discord transport sees it before the next turn,
starts a fresh claude session instead of resuming, and deletes the flag. You lose the
snapshots; you keep everything that matters.

This is NOT a limit. Nothing is counted, no turn is cut short, no work stops — YOU
decide when a wave is done, and the next wave begins immediately in a clean session.
Per-JOB resets were considered and rejected: a fresh session re-reads CLAUDE.md and
profile.yaml before acting, and discards the form-handling patterns (react-select
quirks, PIN-gate timing) that make the later applies in a wave much faster than the
first one.

    python3 scripts/wave.py end      # next turn starts fresh
    python3 scripts/wave.py cancel   # changed your mind — stay in this session
    python3 scripts/wave.py status
"""
from __future__ import annotations

import sys
from pathlib import Path

FLAG = Path(__file__).resolve().parent.parent / "data" / "session_reset.flag"


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "end":
        FLAG.parent.mkdir(parents=True, exist_ok=True)
        FLAG.write_text("wave ended — next turn starts a fresh session\n")
        print("OK — next turn starts with fresh context. Durable state is untouched.")
        return 0
    if cmd == "cancel":
        FLAG.unlink(missing_ok=True)
        print("OK — staying in the current session.")
        return 0
    if cmd == "status":
        print("PENDING — next turn starts fresh" if FLAG.exists() else "NONE — next turn resumes")
        return 0
    print("usage: wave.py {end|cancel|status}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
