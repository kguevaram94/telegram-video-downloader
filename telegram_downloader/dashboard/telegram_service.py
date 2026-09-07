"""Acceso a Telegram para listar mensajes y descargas manuales desde el panel."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from telethon import TelegramClient
from telethon.tl.custom.message import Message

from telegram_downloader.config import AppConfig
from telegram_downloader.download_manager import DownloadManager
from telegram_downloader.filenames import find_message_file, get_original_filename
from telegram_downloader.formatting import format_size
from telegram_downloader.state import load_downloaded_ids
from telegram_downloader.video import is_video_message

T = TypeVar("T")


class TelegramSessionBusyError(RuntimeError):
    """La sesión de Telegram está en uso por el descargador automático."""


class TelegramNotConfiguredError(RuntimeError):
    """Falta CHAT_ID u otra configuración requerida."""


@dataclass(frozen=True)
class VideoMessageSummary:
    message_id: int
    date: str
    filename: str
    size_bytes: int | None
    size_label: str
    caption: str | None
    downloaded: bool
    file_exists: bool
    file_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "date": self.date,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "size_label": self.size_label,
            "caption": self.caption,
            "downloaded": self.downloaded,
            "file_exists": self.file_exists,
            "file_path": self.file_path,
        }


def _is_session_busy(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.OperationalError):
        return "locked" in str(exc).lower()
    return False


async def _with_client(
    config: AppConfig,
    action: Callable[[TelegramClient], Awaitable[T]],
) -> T:
    if config.chat_id is None:
        raise TelegramNotConfiguredError("CHAT_ID no configurado en config.env")

    client = TelegramClient(
        config.session_name,
        config.api_id,
        config.api_hash,
        device_model=config.notify_app_name,
    )

    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Sesión de Telegram no autorizada. Ejecuta downloader.py.")
        return await action(client)
    except Exception as exc:
        if _is_session_busy(exc):
            raise TelegramSessionBusyError(
                "La sesión está en uso por el descargador automático. "
                "Detén el servicio para explorar mensajes o usa la cola de descarga manual."
            ) from exc
        raise
    finally:
        if client.is_connected():
            await client.disconnect()


def _summarize_message(
    message: Message,
    config: AppConfig,
    downloaded_ids: set[int],
) -> VideoMessageSummary:
    existing = find_message_file(message, config.download_dir)
    size_bytes = message.file.size if message.file else None
    caption = (message.text or "").strip() or None

    return VideoMessageSummary(
        message_id=message.id,
        date=message.date.isoformat(),
        filename=get_original_filename(message),
        size_bytes=size_bytes,
        size_label=format_size(size_bytes),
        caption=caption,
        downloaded=message.id in downloaded_ids,
        file_exists=existing is not None,
        file_path=str(existing) if existing is not None else None,
    )


async def list_video_messages(
    config: AppConfig,
    *,
    limit: int = 100,
) -> list[VideoMessageSummary]:
    downloaded_ids = load_downloaded_ids(config.state_file)
    chat_id = config.chat_id
    if chat_id is None:
        raise TelegramNotConfiguredError("CHAT_ID no configurado en config.env")

    async def action(client: TelegramClient) -> list[VideoMessageSummary]:
        summaries: list[VideoMessageSummary] = []
        async for message in client.iter_messages(chat_id, limit=limit):
            if not is_video_message(message):
                continue
            summaries.append(_summarize_message(message, config, downloaded_ids))
        return summaries

    return await _with_client(config, action)


async def download_messages_directly(
    config: AppConfig,
    message_ids: list[int],
    replace_ids: set[int],
) -> dict[str, Any]:
    downloaded_ids = load_downloaded_ids(config.state_file)
    manager = DownloadManager(config, downloaded_ids)
    chat_id = config.chat_id
    if chat_id is None:
        raise TelegramNotConfiguredError("CHAT_ID no configurado en config.env")

    async def action(client: TelegramClient) -> dict[str, Any]:
        worker_task = asyncio.create_task(manager.worker())
        queued = 0
        skipped: list[int] = []

        try:
            for message_id in message_ids:
                replace = message_id in replace_ids
                queued_ok = await manager.enqueue_by_id(
                    client,
                    chat_id,
                    message_id,
                    replace=replace,
                )
                if queued_ok:
                    queued += 1
                else:
                    skipped.append(message_id)

            if queued:
                await manager.wait_until_idle()
        finally:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass

        return {
            "queued": queued,
            "skipped": skipped,
        }

    return await _with_client(config, action)
