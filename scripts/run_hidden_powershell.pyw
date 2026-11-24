"""Run one project PowerShell worker with no console window."""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys


SCRIPTS_DIRECTORY = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIRECTORY.parent
ERROR_LOG = ROOT / "work" / "hidden-task-launcher.log"


def log_error(message: str) -> None:
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ERROR_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now(UTC).isoformat()} {message[:1000]}\n")


def main() -> int:
    if len(sys.argv) < 2:
        raise RuntimeError("A PowerShell script path is required")
    script = Path(sys.argv[1]).resolve()
    if script.parent != SCRIPTS_DIRECTORY or script.suffix.casefold() != ".ps1":
        raise RuntimeError("The target must be a project PowerShell script")
    if not script.is_file():
        raise RuntimeError("The requested PowerShell script does not exist")
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = (
        system_root
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.is_file():
        raise RuntimeError("Windows PowerShell was not found")
    command = [
        str(powershell),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *sys.argv[2:],
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=False,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        try:
            log_error(f"{type(exc).__name__}: {exc}")
        finally:
            raise SystemExit(1)
