"""Start the tracker in the background - what the Windows login shortcut runs.

Runs under pythonw.exe, which has no console window: nothing appears at login, and there is no
window to close by accident and take the site down. With no console there is no stdout/stderr
either, so both are pointed at log files before uvicorn sets up its logging.

Safe to run twice. If something is already listening on the port it exits quietly instead of
fighting for it, so a second login, a manual start or a leftover shortcut does no harm.

Keeps the previous run's logs as server.log.1 / server.err.log.1.

For interactive use - a visible window, Ctrl+C to stop - keep using run.bat.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8765


def already_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((HOST, PORT)) == 0


def _log(name: str):
    path = ROOT / name
    try:
        if path.exists():
            path.replace(ROOT / f"{name}.1")
        mode = "w"
    except OSError:  # held open by something else - keep it and append rather than fail
        mode = "a"
    return open(path, mode, encoding="utf-8", buffering=1)


def main() -> None:
    if already_running():
        return
    sys.stdout = _log("server.log")
    sys.stderr = _log("server.err.log")
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))

    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
