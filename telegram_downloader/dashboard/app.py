"""API FastAPI del panel de estado local."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TypedDict

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from telegram_downloader.config import AppConfig, load_config
from telegram_downloader.dashboard.logs import read_log_tail, read_progress_line
from telegram_downloader.dashboard.service import (
    LaunchAgentStatus,
    ProcessStatus,
    ServicePaths,
    build_service_paths,
    get_downloader_process,
    get_launch_agent_status,
    run_command,
)


class StatusPayload(TypedDict):
    process: ProcessStatus
    launch_agent: LaunchAgentStatus
    download_dir: str
    log_path: str
    progress_line: str | None
    recent_log: list[str]
    healthy: bool


def create_app(config: AppConfig, service_paths: ServicePaths) -> FastAPI:
    app = FastAPI(title="Telegram Video Downloader")
    app.mount("/static", StaticFiles(directory=config.static_dir), name="static")

    def build_status() -> StatusPayload:
        process = get_downloader_process()
        launch_agent = get_launch_agent_status(service_paths)
        return {
            "process": process,
            "launch_agent": launch_agent,
            "download_dir": str(config.download_dir),
            "log_path": str(config.log_path),
            "progress_line": read_progress_line(config.progress_file),
            "recent_log": read_log_tail(config.log_path, max_lines=8),
            "healthy": process["running"] and launch_agent["loaded"],
        }

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(config.static_dir / "index.html")

    @app.get("/api/status")
    async def status() -> JSONResponse:
        return JSONResponse(build_status())

    @app.get("/api/logs")
    async def logs() -> JSONResponse:
        return JSONResponse({
            "lines": read_log_tail(config.log_path, max_lines=500),
            "progress": read_progress_line(config.progress_file),
        })

    @app.get("/api/progress")
    async def progress() -> JSONResponse:
        return JSONResponse({"line": read_progress_line(config.progress_file)})

    @app.get("/api/logs/stream")
    async def logs_stream() -> StreamingResponse:
        async def event_generator():
            last_size = 0
            if config.log_path.exists():
                last_size = config.log_path.stat().st_size
                for line in read_log_tail(config.log_path, max_lines=200):
                    payload = json.dumps({"line": line}, ensure_ascii=False)
                    yield f"data: {payload}\n\n"

            while True:
                await asyncio.sleep(0.5)
                if not config.log_path.exists():
                    continue

                size = config.log_path.stat().st_size
                if size < last_size:
                    last_size = 0

                if size == last_size:
                    continue

                with config.log_path.open("r", errors="replace") as handle:
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
        if not config.download_dir.exists():
            config.download_dir.mkdir(parents=True, exist_ok=True)
        run_command(["open", str(config.download_dir)])
        return JSONResponse({"ok": True})

    @app.post("/api/service/start")
    async def service_start() -> JSONResponse:
        if not service_paths.plist_path.exists():
            return JSONResponse(
                {"ok": False, "error": "LaunchAgent no instalado"},
                status_code=400,
            )

        run_command(["launchctl", "bootout", service_paths.service_label])
        result = run_command([
            "launchctl",
            "bootstrap",
            f"gui/{os.getuid()}",
            str(service_paths.plist_path),
        ])
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            return JSONResponse({"ok": False, "error": error}, status_code=500)
        return JSONResponse({"ok": True, "status": build_status()})

    @app.post("/api/service/stop")
    async def service_stop() -> JSONResponse:
        result = run_command(["launchctl", "bootout", service_paths.service_label])
        if result.returncode != 0 and "No such process" not in result.stderr:
            error = result.stderr.strip() or result.stdout.strip()
            return JSONResponse({"ok": False, "error": error}, status_code=500)
        return JSONResponse({"ok": True, "status": build_status()})

    @app.post("/api/service/restart")
    async def service_restart() -> JSONResponse:
        kick = run_command(["launchctl", "kickstart", "-k", service_paths.service_label])
        if kick.returncode == 0:
            return JSONResponse({"ok": True, "status": build_status()})
        return await service_start()

    return app


_config = load_config()
_service_paths = build_service_paths()
app = create_app(_config, _service_paths)


def main() -> None:
    uvicorn.run(
        "telegram_downloader.dashboard.app:app",
        host=_config.dashboard_host,
        reload=False,
        log_level="info",
        port=_config.dashboard_port,
    )
