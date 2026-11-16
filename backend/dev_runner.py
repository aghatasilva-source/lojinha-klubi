"""Supervisor local: mantém a API ativa e recarrega o backend quando o código muda."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def watched_files() -> dict[str, int]:
    files = list((ROOT / "backend").rglob("*.py"))
    for extra in (ROOT / "sheets.json", ROOT / ".env"):
        if extra.exists():
            files.append(extra)
    return {str(path): path.stat().st_mtime_ns for path in files}


def start_backend() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "backend.app.main"], cwd=ROOT)


def stop_backend(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> None:
    process = start_backend()
    snapshot = watched_files()
    try:
        while True:
            time.sleep(0.8)
            current = watched_files()
            if current != snapshot:
                snapshot = current
                stop_backend(process)
                process = start_backend()
            elif process.poll() is not None:
                process = start_backend()
    except KeyboardInterrupt:
        stop_backend(process)


if __name__ == "__main__":
    main()
