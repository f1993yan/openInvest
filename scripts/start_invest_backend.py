"""Portable OpenInvest backend launcher for Windows double-click startup."""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR_DEFAULT = ROOT / "logs"
MONITOR_LOG_DIR = ROOT / "data" / "market_monitor"
PROCESS_PATTERNS = (
    "backend.server:app",
    "jobs.market_monitor",
    "scheduler.runner",
    "scripts.monitor_desktop_window",
)


@dataclass(frozen=True)
class LaunchConfig:
    root: Path
    uv: str
    host: str
    port: int | None
    log_dir: Path
    restart_existing: bool
    start_backend: bool
    start_window: bool
    window_poll_ms: int


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def build_config(args: argparse.Namespace) -> LaunchConfig:
    root = Path(os.getenv("OPENINVEST_ROOT") or args.root or ROOT).resolve()
    uv = os.getenv("OPENINVEST_UV") or args.uv or shutil.which("uv") or "uv"
    log_dir = Path(os.getenv("OPENINVEST_LOG_DIR") or args.log_dir or (root / "logs"))
    if not log_dir.is_absolute():
        log_dir = root / log_dir
    return LaunchConfig(
        root=root,
        uv=uv,
        host=os.getenv("INVEST_BACKEND_HOST") or args.host,
        port=_resolve_backend_port(args),
        log_dir=log_dir.resolve(),
        restart_existing=env_bool("OPENINVEST_RESTART_EXISTING", not args.no_restart),
        start_backend=env_bool("OPENINVEST_START_BACKEND", args.with_backend),
        start_window=env_bool("OPENINVEST_START_WINDOW", not args.no_window),
        window_poll_ms=env_int("OPENINVEST_WINDOW_POLL_MS", args.window_poll_ms),
    )


def _resolve_backend_port(args: argparse.Namespace) -> int | None:
    value = os.getenv("INVEST_BACKEND_PORT")
    if value:
        try:
            return int(value)
        except ValueError:
            return None
    return args.port


def ensure_layout(config: LaunchConfig) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    (config.root / "data" / "market_monitor").mkdir(parents=True, exist_ok=True)
    (config.root / "data" / "weekend_news").mkdir(parents=True, exist_ok=True)
    if not (config.root / "pyproject.toml").exists():
        raise SystemExit(f"OpenInvest root is invalid: {config.root}")
    if config.start_backend and shutil.which(config.uv) is None and not Path(config.uv).exists():
        raise SystemExit(
            "uv was not found. Install uv or set OPENINVEST_UV to its full path.\n"
            "Example: set OPENINVEST_UV=C:\\Users\\you\\.local\\bin\\uv.exe"
        )


def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        text=True,
        capture_output=True,
        check=False,
    )


def stop_existing_processes(config: LaunchConfig) -> None:
    quoted_root = str(config.root).replace("'", "''").lower()
    patterns = ",".join(f"'{pattern}'" for pattern in PROCESS_PATTERNS)
    script = f"""
$root = '{quoted_root}'
$patterns = @({patterns})
$all = @(Get-CimInstance Win32_Process)
$byPid = @{{}}
foreach ($proc in $all) {{ $byPid[[int]$proc.ProcessId] = $proc }}
$seedTargets = @($all | Where-Object {{
  $cmd = ($_.CommandLine + '').ToLowerInvariant()
  $exe = ($_.ExecutablePath + '').ToLowerInvariant()
  $name = ($_.Name + '').ToLowerInvariant()
  ($name -match '^(cmd|uv|uvicorn|python|pythonw)\\.exe$') -and
  ($cmd.Contains($root) -or $exe.Contains($root)) -and
  ($patterns | Where-Object {{ $cmd.Contains($_) }})
}})
$targetMap = @{{}}
foreach ($target in $seedTargets) {{
  $cursor = $target
  while ($null -ne $cursor) {{
    $name = ($cursor.Name + '').ToLowerInvariant()
    if ($name -notmatch '^(cmd|uv|uvicorn|python|pythonw)\\.exe$') {{ break }}
    $targetMap[[int]$cursor.ProcessId] = $cursor
    if (-not $byPid.ContainsKey([int]$cursor.ParentProcessId)) {{ break }}
    $parent = $byPid[[int]$cursor.ParentProcessId]
    $parentCmd = ($parent.CommandLine + '').ToLowerInvariant()
    $parentExe = ($parent.ExecutablePath + '').ToLowerInvariant()
    if (-not ($parentCmd.Contains($root) -or $parentExe.Contains($root) -or $parent.Name.ToLowerInvariant() -eq 'uv.exe')) {{ break }}
    $cursor = $parent
  }}
}}
$targets = @($targetMap.Values | Sort-Object ProcessId -Descending)
foreach ($target in $targets) {{
  Write-Host ('Stopping PID {{0}}: {{1}}' -f $target.ProcessId, $target.CommandLine)
  Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}}
foreach ($target in $targets) {{
  Wait-Process -Id $target.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
}}
"""
    result = run_powershell(script)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0 and result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def popen_hidden(
    config: LaunchConfig,
    name: str,
    args: Sequence[str],
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.Popen[bytes]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    print(f"Starting {name}...")
    return subprocess.Popen(
        [config.uv, *args],
        cwd=config.root,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
        close_fds=True,
    )


def popen_python_hidden(
    config: LaunchConfig,
    name: str,
    module: str,
    stdout_path: Path,
    stderr_path: Path,
    extra_args: Sequence[str] = (),
) -> subprocess.Popen[bytes]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    print(f"Starting {name}...")
    return subprocess.Popen(
        [sys.executable, "-m", module, *extra_args],
        cwd=config.root,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=creationflags,
        close_fds=True,
    )


def is_port_open(host: str, port: int, timeout_sec: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, deadline_sec: float = 12.0) -> bool:
    deadline = time.monotonic() + deadline_sec
    while time.monotonic() < deadline:
        if is_port_open(host, port):
            return True
        time.sleep(0.5)
    return False


def launch_window(config: LaunchConfig) -> None:
    print("Starting desktop monitor window...")
    args = [
        "-m",
        "scripts.launch_monitor_window",
        "--poll-ms",
        str(config.window_poll_ms),
    ]
    result = subprocess.run(
        [sys.executable, *args],
        cwd=config.root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.returncode != 0:
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit("Desktop monitor window failed to start.")


def launch(config: LaunchConfig) -> None:
    ensure_layout(config)
    print(f"OpenInvest root: {config.root}")
    print(f"Using Python: {sys.executable}")
    if config.restart_existing:
        print("Stopping previous OpenInvest processes if any...")
        stop_existing_processes(config)

    backend: subprocess.Popen[bytes] | None = None
    if config.start_backend:
        if config.port is None:
            raise SystemExit("Optional HTTP backend requires --port or INVEST_BACKEND_PORT.")
        backend = popen_hidden(
            config,
            "optional HTTP backend",
            ["run", "uvicorn", "backend.server:app", "--host", config.host, "--port", str(config.port)],
            config.log_dir / "backend.log",
            config.log_dir / "backend.err.log",
        )
    else:
        print("Skipping optional HTTP backend. Desktop window uses local files and direct Python calls.")

    popen_python_hidden(
        config,
        "market monitor",
        "jobs.market_monitor",
        config.root / "data" / "market_monitor" / "monitor_stdout.log",
        config.root / "data" / "market_monitor" / "monitor_stderr.log",
    )
    popen_python_hidden(
        config,
        "job scheduler",
        "scheduler.runner",
        config.log_dir / "scheduler.log",
        config.log_dir / "scheduler.err.log",
    )

    if config.start_window:
        launch_window(config)

    if backend is not None:
        if wait_for_port(config.host, config.port):
            print(f"Optional HTTP backend ready on {config.host}:{config.port}")
        else:
            print(
                f"Optional HTTP backend started but port {config.port} was not ready yet. "
                f"Check {config.log_dir / 'backend.err.log'} if it does not recover."
            )
            if backend.poll() is not None:
                print(f"Optional HTTP backend exited early with code {backend.returncode}.", file=sys.stderr)
    print("Startup finished. You can close this window.")


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start OpenInvest backend services")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--uv", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument("--with-backend", action="store_true", help="Start the optional HTTP backend on INVEST_BACKEND_PORT")
    parser.add_argument("--no-window", action="store_true")
    parser.add_argument("--window-poll-ms", type=int, default=1000)
    return parser.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    launch(build_config(args))


if __name__ == "__main__":
    main()
