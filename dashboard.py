#!/usr/bin/env python3
"""Panel local para ver estado, logs y control del descargador."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
LOG_PATH = ROOT / "downloader.log"
PROGRESS_PATH = ROOT / "downloader.progress"
PLIST_PATH = Path.home() / "Library/LaunchAgents/com.telegram.video-downloader.plist"
SERVICE_LABEL = f"gui/{os.getuid()}/com.telegram.video-downloader"

load_dotenv(ROOT / "config.env")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(Path.home() / "Videos" / "Telegram")))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8765"))

app = FastAPI(title="Telegram Video Downloader")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True)


def read_log_tail(max_lines: int = 200) -> list[str]:
    if not LOG_PATH.exists():
        return ["(sin log todavía)"]

    lines = LOG_PATH.read_text(errors="replace").splitlines()
    if not lines:
        return ["(log vacío)"]

    progress_re = re.compile(r"(?:\[\+\d+ en cola\] )?.+: *\d+% ")
    filtered = [line for line in lines if not progress_re.search(line)]
    if not filtered:
        return ["(log vacío)"]
    return filtered[-max_lines:]


def read_progress_line() -> Optional[str]:
    if not PROGRESS_PATH.exists():
        return None

    line = PROGRESS_PATH.read_text(errors="replace").strip()
    return line or None


def get_downloader_process() -> dict[str, Any]:
    result = run_command(["pgrep", "-fl", "downloader.py"])
    if result.returncode != 0 or not result.stdout.strip():
        return {"running": False, "pid": None, "command": None}

    line = result.stdout.strip().splitlines()[0]
    parts = line.split(maxsplit=1)
    pid = parts[0]
    command = parts[1] if len(parts) > 1 else None
    return {"running": True, "pid": int(pid), "command": command}


def get_launch_agent_status() -> dict[str, Any]:
    result = run_command(["launchctl", "print", SERVICE_LABEL])
    installed = PLIST_PATH.exists()
    running = result.returncode == 0

    state = None
    pid = None
    if running:
        for line in result.stdout.splitlines():
            if line.strip().startswith("state ="):
                state = line.split("=", 1)[1].strip()
            if line.strip().startswith("pid ="):
                value = line.split("=", 1)[1].strip()
                pid = int(value) if value.isdigit() else None

    return {
        "installed": installed,
        "loaded": running,
        "state": state,
        "pid": pid,
        "label": SERVICE_LABEL,
        "plist_path": str(PLIST_PATH),
    }


def build_status() -> dict[str, Any]:
    process = get_downloader_process()
    launch_agent = get_launch_agent_status()
    log_lines = read_log_tail(8)

    return {
        "process": process,
        "launch_agent": launch_agent,
        "download_dir": str(DOWNLOAD_DIR),
        "log_path": str(LOG_PATH),
        "progress_line": read_progress_line(),
        "recent_log": log_lines,
        "healthy": process["running"] and launch_agent["loaded"],
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def status() -> JSONResponse:
    return JSONResponse(build_status())


@app.get("/api/logs")
async def logs() -> JSONResponse:
    return JSONResponse({"lines": read_log_tail(500), "progress": read_progress_line()})


@app.get("/api/progress")
async def progress() -> JSONResponse:
    return JSONResponse({"line": read_progress_line()})


@app.get("/api/logs/stream")
async def logs_stream() -> StreamingResponse:
    async def event_generator():
        last_size = 0
        if LOG_PATH.exists():
            last_size = LOG_PATH.stat().st_size
            for line in read_log_tail(200):
                payload = json.dumps({"line": line}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

        while True:
            await asyncio.sleep(0.5)
            if not LOG_PATH.exists():
                continue

            size = LOG_PATH.stat().st_size
            if size < last_size:
                last_size = 0

            if size == last_size:
                continue

            with LOG_PATH.open("r", errors="replace") as handle:
                handle.seek(last_size)
                chunk = handle.read()
                last_size = handle.tell()

            for line in chunk.splitlines():
                payload = json.dumps({"line": line}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/api/open-downloads")
async def open_downloads() -> JSONResponse:
    if not DOWNLOAD_DIR.exists():
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    run_command(["open", str(DOWNLOAD_DIR)])
    return JSONResponse({"ok": True})


@app.post("/api/service/start")
async def service_start() -> JSONResponse:
    if not PLIST_PATH.exists():
        return JSONResponse({"ok": False, "error": "LaunchAgent no instalado"}, status_code=400)

    run_command(["launchctl", "bootout", SERVICE_LABEL])
    result = run_command(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST_PATH)])
    if result.returncode != 0:
        return JSONResponse({"ok": False, "error": result.stderr.strip() or result.stdout.strip()}, status_code=500)
    return JSONResponse({"ok": True, "status": build_status()})


@app.post("/api/service/stop")
async def service_stop() -> JSONResponse:
    result = run_command(["launchctl", "bootout", SERVICE_LABEL])
    if result.returncode != 0 and "No such process" not in result.stderr:
        return JSONResponse({"ok": False, "error": result.stderr.strip() or result.stdout.strip()}, status_code=500)
    return JSONResponse({"ok": True, "status": build_status()})


@app.post("/api/service/restart")
async def service_restart() -> JSONResponse:
    kick = run_command(["launchctl", "kickstart", "-k", SERVICE_LABEL])
    if kick.returncode == 0:
        return JSONResponse({"ok": True, "status": build_status()})

    start = await service_start()
    return start


def main() -> None:
    uvicorn.run(
        "dashboard:app",
        host=DASHBOARD_HOST,
        reload=False,
        log_level="info",
        port=DASHBOARD_PORT,
    )


if __name__ == "__main__":
    main()
