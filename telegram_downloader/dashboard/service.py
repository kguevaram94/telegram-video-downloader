"""Estado del proceso y LaunchAgent del descargador."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


class ProcessStatus(TypedDict):
    running: bool
    pid: int | None
    command: str | None


class LaunchAgentStatus(TypedDict):
    installed: bool
    loaded: bool
    state: str | None
    pid: int | None
    label: str
    plist_path: str


@dataclass(frozen=True)
class ServicePaths:
    plist_path: Path
    service_label: str


def build_service_paths() -> ServicePaths:
    plist_path = (
        Path.home() / "Library/LaunchAgents/com.telegram.video-downloader.plist"
    )
    label = f"gui/{os.getuid()}/com.telegram.video-downloader"
    return ServicePaths(plist_path=plist_path, service_label=label)


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


def get_downloader_process() -> ProcessStatus:
    result = run_command(["pgrep", "-fl", "downloader.py"])
    if result.returncode != 0 or not result.stdout.strip():
        return {"running": False, "pid": None, "command": None}

    line = result.stdout.strip().splitlines()[0]
    parts = line.split(maxsplit=1)
    pid = int(parts[0])
    command = parts[1] if len(parts) > 1 else None
    return {"running": True, "pid": pid, "command": command}


def _parse_launchctl_pid(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def get_launch_agent_status(paths: ServicePaths) -> LaunchAgentStatus:
    result = run_command(["launchctl", "print", paths.service_label])
    installed = paths.plist_path.exists()
    loaded = result.returncode == 0

    state: str | None = None
    pid: int | None = None
    if loaded:
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("state ="):
                state = stripped.split("=", 1)[1].strip()
            if stripped.startswith("pid ="):
                pid = _parse_launchctl_pid(stripped.split("=", 1)[1].strip())

    return {
        "installed": installed,
        "loaded": loaded,
        "state": state,
        "pid": pid,
        "label": paths.service_label,
        "plist_path": str(paths.plist_path),
    }
