"""Entry point for the packaged macOS app: no terminal, log to a file."""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path


def _open_log():
    log_dir = Path.home() / "Library" / "Logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return open(log_dir / "session-sync.log", "a", buffering=1, encoding="utf-8")


def main() -> int:
    log = None
    if sys.stdout is None or sys.stderr is None:
        log = _open_log()
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    if os.getcwd() == "/":
        os.chdir(Path.home())
    if log:
        print(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch ===", flush=True)
    try:
        from session_sync.websrv import run_ui

        return run_ui(open_window=os.environ.get("SESSION_SYNC_NO_OPEN") != "1")
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
