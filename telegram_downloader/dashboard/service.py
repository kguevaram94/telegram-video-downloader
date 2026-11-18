"""Estado y control del descargador (LaunchAgent en macOS o proceso en Docker/Linux)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from telegram_downloader.config import ROOT

ServiceBackend = Literal["launchd", "process"]


class ProcessStatus(TypedDict):
    running: bool
    pid: int | None
    command: str | None


class SupervisorStatus(TypedDict):
    backend: ServiceBackend
    installed: bool
    loaded: bool
    state: str | None
    pid: int | None
    label: str
    detail: str


# Compatibilidad con el panel anterior (misma forma + backend).
LaunchAgentStatus = SupervisorStatus


@dataclass(frozen=True)
class ServicePaths:
    backend: ServiceBackend
    plist_path: Path
    service_label: str
    downloader_script: Path
    log_path: Path


def detect_service_backend() -> ServiceBackend:
    explicit = os.getenv("SERVICE_BACKEND", "").strip().lower()
    if explicit == "launchd":
        return "launchd"
    if explicit == "process":
        return "process"
    return "launchd" if sys.platform == "darwin" else "process"


def build_service_paths(*, log_path: Path | None = None) -> ServicePaths:
    backend = detect_service_backend()
    plist_path = (
        Path.home() / "Library/LaunchAgents/com.telegram.video-downloader.plist"
    )
    label = f"gui/{os.getuid()}/com.telegram.video-downloader"
    return ServicePaths(
        backend=backend,
        plist_path=plist_path,
        service_label=label,
        downloader_script=ROOT / "downloader.py",
        log_path=log_path or (ROOT / "downloader.log"),
    )


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


def get_downloader_process() -> ProcessStatus:
    result = run_command(["pgrep", "-fl", "downloader.py"])
    if result.returncode != 0 or not result.stdout.strip():
        return {"running": False, "pid": None, "command": None}

    for line in result.stdout.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue
        command = parts[1]
        if "pgrep" in command:
            continue
        return {"running": True, "pid": int(parts[0]), "command": command}

    return {"running": False, "pid": None, "command": None}


def _parse_launchctl_pid(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def get_launch_agent_status(paths: ServicePaths) -> SupervisorStatus:
    if paths.backend == "process":
        return _get_process_supervisor_status(paths)
    return _get_launchd_supervisor_status(paths)


def _get_process_supervisor_status(paths: ServicePaths) -> SupervisorStatus:
    process = get_downloader_process()
    return {
        "backend": "process",
        "installed": paths.downloader_script.exists(),
        "loaded": process["running"],
        "state": "running" if process["running"] else "stopped",
        "pid": process["pid"],
        "label": "process/downloader.py",
        "detail": str(paths.downloader_script),
    }


def _get_launchd_supervisor_status(paths: ServicePaths) -> SupervisorStatus:
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
        "backend": "launchd",
        "installed": installed,
        "loaded": loaded,
        "state": state,
        "pid": pid,
        "label": paths.service_label,
        "detail": str(paths.plist_path),
    }


def start_downloader(paths: ServicePaths) -> tuple[bool, str | None]:
    if paths.backend == "process":
        return _start_process(paths)
    return _start_launchd(paths)


def stop_downloader(paths: ServicePaths) -> tuple[bool, str | None]:
    if paths.backend == "process":
        return _stop_process()
    return _stop_launchd(paths)


def restart_downloader(paths: ServicePaths) -> tuple[bool, str | None]:
    if paths.backend == "process":
        ok, error = _stop_process()
        if not ok:
            return False, error
        time.sleep(0.4)
        return _start_process(paths)

    kick = run_command(["launchctl", "kickstart", "-k", paths.service_label])
    if kick.returncode == 0:
        return True, None
    return _start_launchd(paths)


def _start_launchd(paths: ServicePaths) -> tuple[bool, str | None]:
    if not paths.plist_path.exists():
        return False, "LaunchAgent no instalado"

    run_command(["launchctl", "bootout", paths.service_label])
    result = run_command([
        "launchctl",
        "bootstrap",
        f"gui/{os.getuid()}",
        str(paths.plist_path),
    ])
    if result.returncode != 0:
        return False, result.stderr.strip() or result.stdout.strip() or "Error al iniciar"
    return True, None


def _stop_launchd(paths: ServicePaths) -> tuple[bool, str | None]:
    result = run_command(["launchctl", "bootout", paths.service_label])
    if result.returncode != 0 and "No such process" not in result.stderr:
        return False, result.stderr.strip() or result.stdout.strip() or "Error al detener"
    return True, None


def _start_process(paths: ServicePaths) -> tuple[bool, str | None]:
    if get_downloader_process()["running"]:
        return True, None

    if not paths.downloader_script.exists():
        return False, f"No existe {paths.downloader_script}"

    paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_handle = paths.log_path.open("a", encoding="utf-8")
    except OSError as exc:
        return False, f"No se pudo abrir el log: {exc}"

    try:
        subprocess.Popen(
            [sys.executable, "-u", str(paths.downloader_script)],
            cwd=str(ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        log_handle.close()
        return False, str(exc)

    log_handle.close()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if get_downloader_process()["running"]:
            return True, None
        time.sleep(0.2)

    return False, "El proceso no arrancó a tiempo"


def _stop_process() -> tuple[bool, str | None]:
    process = get_downloader_process()
    if not process["running"] or process["pid"] is None:
        return True, None

    pid = process["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, None
    except PermissionError as exc:
        return False, str(exc)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not get_downloader_process()["running"]:
            return True, None
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, None
    except PermissionError as exc:
        return False, str(exc)

    return True, None
