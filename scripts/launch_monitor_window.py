"""Launch the standalone monitor window without leaving extra consoles."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"


def stop_existing_monitor_windows() -> None:
    if os.name != "nt":
        return
    script = r"""
$targets = @(Get-CimInstance Win32_Process | Where-Object {
  (($_.Name + '').ToLowerInvariant() -match '^(python|pythonw)\.exe$') -and
  (($_.CommandLine + '').ToLowerInvariant().Contains('scripts.monitor_desktop_window'))
})
foreach ($target in $targets) {
  Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}
foreach ($target in $targets) {
  Wait-Process -Id $target.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
}
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def launch_window(poll_ms: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    command = [
        str(executable),
        "-m",
        "scripts.monitor_desktop_window",
        "--poll-ms",
        str(poll_ms),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if executable.name.lower() != "pythonw.exe":
            creationflags |= subprocess.CREATE_NO_WINDOW
    with (LOG_DIR / "window.err.log").open("ab") as err:
        subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err,
            creationflags=creationflags,
            close_fds=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch OpenInvest monitor window")
    parser.add_argument("--poll-ms", default="1000")
    args = parser.parse_args()
    stop_existing_monitor_windows()
    launch_window(args.poll_ms)


if __name__ == "__main__":
    main()
