"""API FastAPI del panel de estado local."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TypedDict

import uvicorn
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from telegram_downloader.config import AppConfig, load_config
from telegram_downloader.dashboard.logs import read_log_tail, read_progress_line
from telegram_downloader.dashboard.service import (
    ProcessStatus,
    ServicePaths,
    SupervisorStatus,
    build_service_paths,
    get_downloader_process,
    get_launch_agent_status,
    restart_downloader,
    run_command,
    start_downloader,
    stop_downloader,
)
from telegram_downloader.dashboard.stats import (
    format_directory_size,
    get_directory_size_bytes,
    get_downloaded_count,
)
from telegram_downloader.dashboard.telegram_service import (
    TelegramNotConfiguredError,
    TelegramSessionBusyError,
    download_messages_directly,
    list_video_messages,
)
from telegram_downloader.manual_queue import ManualDownloadRequest, append_requests


class StatusPayload(TypedDict):
    process: ProcessStatus
    launch_agent: SupervisorStatus
    supervisor: SupervisorStatus
    download_dir: str
    log_path: str
    progress_line: str | None
    recent_log: list[str]
    healthy: bool
    downloaded_count: int
    download_dir_size_bytes: int | None
    download_dir_size_label: str | None
    can_open_downloads: bool


class DownloadMessagesRequest(BaseModel):
    message_ids: list[int] = Field(min_length=1)
    replace_ids: list[int] = Field(default_factory=list)


def _is_downloader_running() -> bool:
    return get_downloader_process()["running"]


def _is_healthy(process: ProcessStatus, supervisor: SupervisorStatus) -> bool:
    if supervisor["backend"] == "process":
        return process["running"]
    return process["running"] and supervisor["loaded"]


def create_app(config: AppConfig, service_paths: ServicePaths) -> FastAPI:
    app = FastAPI(title="Telegram Video Downloader")
    app.mount("/static", StaticFiles(directory=config.static_dir), name="static")

    def build_status() -> StatusPayload:
        process = get_downloader_process()
        supervisor = get_launch_agent_status(service_paths)
        dir_size = get_directory_size_bytes(config.download_dir)
        return {
            "process": process,
            "launch_agent": supervisor,
            "supervisor": supervisor,
            "download_dir": str(config.download_dir),
            "log_path": str(config.log_path),
            "progress_line": read_progress_line(config.progress_file),
            "recent_log": read_log_tail(config.log_path, max_lines=8),
            "healthy": _is_healthy(process, supervisor),
            "downloaded_count": get_downloaded_count(config.state_file),
            "download_dir_size_bytes": dir_size,
            "download_dir_size_label": format_directory_size(dir_size),
            "can_open_downloads": sys.platform == "darwin",
        }

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            config.static_dir / "index.html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )

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
        if sys.platform != "darwin":
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "Abrir carpeta solo está disponible en macOS. "
                        f"Ruta: {config.download_dir}"
                    ),
                },
                status_code=400,
            )
        if not config.download_dir.exists():
            config.download_dir.mkdir(parents=True, exist_ok=True)
        run_command(["open", str(config.download_dir)])
        return JSONResponse({"ok": True})

    @app.post("/api/service/start")
    async def service_start() -> JSONResponse:
        ok, error = start_downloader(service_paths)
        if not ok:
            return JSONResponse({"ok": False, "error": error}, status_code=500)
        return JSONResponse({"ok": True, "status": build_status()})

    @app.post("/api/service/stop")
    async def service_stop() -> JSONResponse:
        ok, error = stop_downloader(service_paths)
        if not ok:
            return JSONResponse({"ok": False, "error": error}, status_code=500)
        return JSONResponse({"ok": True, "status": build_status()})

    @app.post("/api/service/restart")
    async def service_restart() -> JSONResponse:
        ok, error = restart_downloader(service_paths)
        if not ok:
            return JSONResponse({"ok": False, "error": error}, status_code=500)
        return JSONResponse({"ok": True, "status": build_status()})

    @app.get("/api/messages")
    async def messages(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> JSONResponse:
        try:
            items = await list_video_messages(config, limit=limit)
        except TelegramSessionBusyError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error": str(exc),
                    "session_busy": True,
                },
                status_code=503,
            )
        except TelegramNotConfiguredError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        return JSONResponse({
            "ok": True,
            "messages": [item.to_dict() for item in items],
            "downloader_running": _is_downloader_running(),
        })

    @app.post("/api/messages/download")
    async def download_messages(payload: DownloadMessagesRequest) -> JSONResponse:
        replace_ids = set(payload.replace_ids)
        unique_ids = list(dict.fromkeys(payload.message_ids))

        if _is_downloader_running():
            requests = [
                ManualDownloadRequest(
                    message_id=message_id,
                    replace=message_id in replace_ids,
                )
                for message_id in unique_ids
            ]
            append_requests(config.manual_queue_file, requests)
            return JSONResponse({
                "ok": True,
                "mode": "queued",
                "queued": len(requests),
                "message": (
                    "Descargas encoladas. El servicio automático las procesará en breve."
                ),
            })

        try:
            result = await download_messages_directly(
                config,
                unique_ids,
                replace_ids,
            )
        except TelegramSessionBusyError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc), "session_busy": True},
                status_code=503,
            )
        except TelegramNotConfiguredError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        return JSONResponse({
            "ok": True,
            "mode": "direct",
            "queued": result["queued"],
            "skipped": result["skipped"],
        })

    return app


_config = load_config()
_service_paths = build_service_paths(log_path=_config.log_path)
app = create_app(_config, _service_paths)


def main() -> None:
    uvicorn.run(
        "telegram_downloader.dashboard.app:app",
        host=_config.dashboard_host,
        reload=False,
        log_level="info",
        port=_config.dashboard_port,
    )
