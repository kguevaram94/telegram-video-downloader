"""Cola de descargas y worker asíncrono."""

from __future__ import annotations

import asyncio
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.custom.message import Message

from telegram_downloader.config import AppConfig
from telegram_downloader.filenames import (
    find_message_file,
    get_document_filename,
    get_message_title,
    resolve_target_path,
)
from telegram_downloader.formatting import format_size
from telegram_downloader.manual_queue import ManualDownloadRequest, take_pending
from telegram_downloader.media_classifier import (
    ClassificationResult,
    MediaClassifier,
    build_media_classifier,
)
from telegram_downloader.notifications import notify_async
from telegram_downloader.progress import ProgressFileStore, ProgressReporter
from telegram_downloader.state import save_downloaded_ids
from telegram_downloader.video import is_video_message


class DownloadManager:
    def __init__(self, config: AppConfig, downloaded_ids: set[int]) -> None:
        self._config = config
        self.downloaded_ids = downloaded_ids
        self._queue: asyncio.Queue[tuple[TelegramClient, Message, bool]] = (
            asyncio.Queue()
        )
        self._queued_ids: set[int] = set()
        self._in_progress_ids: set[int] = set()
        self._progress_store = ProgressFileStore(config.progress_file)
        self._progress = ProgressReporter(self._progress_store)
        self._classifier = self._build_classifier(config)

    def queue_waiting(self) -> int:
        return self._queue.qsize()

    async def wait_until_idle(self) -> None:
        await self._queue.join()

    async def enqueue(
        self,
        client: TelegramClient,
        message: Message,
        *,
        replace: bool = False,
    ) -> None:
        if message.id in self.downloaded_ids and not replace:
            return
        if message.id in self._queued_ids or message.id in self._in_progress_ids:
            return

        self._queued_ids.add(message.id)
        await self._queue.put((client, message, replace))
        await self._notify_enqueued(message)

    async def enqueue_by_id(
        self,
        client: TelegramClient,
        chat_id: int,
        message_id: int,
        *,
        replace: bool = False,
    ) -> bool:
        message = await client.get_messages(chat_id, ids=message_id)
        if message is None or not is_video_message(message):
            return False
        await self.enqueue(client, message, replace=replace)
        return True

    async def worker(self) -> None:
        while True:
            client, message, replace = await self._queue.get()
            self._queued_ids.discard(message.id)
            self._in_progress_ids.add(message.id)

            try:
                await self._download_one(client, message, replace=replace)
            except Exception as exc:
                self._progress_store.clear()
                self._progress.finish(f"Error mensaje {message.id}: {exc}")
                await self._notify_error(message.id, exc)
            finally:
                self._in_progress_ids.discard(message.id)
                self._queue.task_done()

    async def poll_manual_queue(
        self,
        client: TelegramClient,
        chat_id: int,
    ) -> None:
        while True:
            await asyncio.sleep(2)
            pending = take_pending(self._config.manual_queue_file)
            for request in pending:
                await self._enqueue_manual_request(client, chat_id, request)

    async def _enqueue_manual_request(
        self,
        client: TelegramClient,
        chat_id: int,
        request: ManualDownloadRequest,
    ) -> None:
        try:
            await self.enqueue_by_id(
                client,
                chat_id,
                request.message_id,
                replace=request.replace,
            )
        except Exception as exc:
            self._progress.finish(
                f"Error encolando mensaje {request.message_id}: {exc}"
            )

    async def backfill(
        self,
        client: TelegramClient,
        chat_id: int,
        limit: int,
    ) -> None:
        print(f"Buscando hasta {limit} mensajes recientes con video...", flush=True)
        found = 0

        async for message in client.iter_messages(chat_id, limit=limit):
            if not is_video_message(message):
                continue
            await self.enqueue(client, message)
            found += 1

        if found:
            await self._queue.join()
            print(f"Backfill completado: {found} video(s) procesado(s).", flush=True)
            return

        print("Backfill completado: no habia videos recientes.", flush=True)

    async def _notify_enqueued(self, message: Message) -> None:
        dest = resolve_target_path(message, self._config.download_dir)
        total_size = message.file.size if message.file else None
        waiting = self.queue_waiting()
        queue_note = f" (+{waiting} en cola)" if waiting else ""
        await notify_async(
            "Video detectado",
            f"{dest.name} ({format_size(total_size)}){queue_note}",
            enabled=self._config.notify_enabled,
            app_name=self._config.notify_app_name,
        )

    async def _notify_error(self, message_id: int, exc: Exception) -> None:
        await notify_async(
            "Error de descarga",
            f"Mensaje {message_id}: {exc}",
            enabled=self._config.notify_enabled,
            app_name=self._config.notify_app_name,
        )

    async def _download_one(
        self,
        client: TelegramClient,
        message: Message,
        *,
        replace: bool = False,
    ) -> None:
        if message.id in self.downloaded_ids and not replace:
            return

        if replace:
            self._remove_existing_download(message)

        dest = resolve_target_path(message, self._config.download_dir)
        await self._download_to_inbox(client, message, dest)
        final_path = await self._classify_downloaded_file(dest, message)
        self._mark_downloaded(message.id)
        await self._notify_completed(final_path)

    async def _download_to_inbox(
        self,
        client: TelegramClient,
        message: Message,
        dest: Path,
    ) -> None:
        total_size = message.file.size if message.file else None
        temp_dest = dest.with_suffix(dest.suffix + ".part")
        if temp_dest.exists():
            temp_dest.unlink()

        self._progress.reset_pct()
        cola = f"[+{self.queue_waiting()} en cola] " if self.queue_waiting() else ""
        self._progress.status(
            f"{cola}{dest.name}:   0% (0 MB / {format_size(total_size)})"
        )

        await client.download_media(
            message,
            file=str(temp_dest),
            progress_callback=self._progress.make_callback(
                dest.name,
                self.queue_waiting,
            ),
        )
        temp_dest.replace(dest)

    def _mark_downloaded(self, message_id: int) -> None:
        self.downloaded_ids.add(message_id)
        save_downloaded_ids(self._config.state_file, self.downloaded_ids)

    async def _notify_completed(self, final_path: Path) -> None:
        file_size = final_path.stat().st_size
        self._progress.finish(
            f"Completado: {final_path.name} ({format_size(file_size)})"
        )
        await notify_async(
            "Descarga completada",
            f"{final_path.name} ({format_size(file_size)})",
            enabled=self._config.notify_enabled,
            app_name=self._config.notify_app_name,
        )

    async def _classify_downloaded_file(
        self,
        dest: Path,
        message: Message,
    ) -> Path:
        if self._classifier is None:
            return dest

        try:
            result = await self._classifier.classify_and_relocate(
                dest,
                message_text=get_message_title(message),
                original_filename=get_document_filename(message),
            )
        except Exception as exc:
            print(
                f"[TMDB] Clasificación omitida: {dest.name} "
                f"| error={exc.__class__.__name__}",
                flush=True,
            )
            self._progress.status(
                f"Clasificación omitida ({exc.__class__.__name__}): se mantiene Inbox"
            )
            return dest

        return self._path_after_classification(dest, result)

    def _path_after_classification(
        self,
        dest: Path,
        result: ClassificationResult,
    ) -> Path:
        if result.status == "classified" and result.destination is not None:
            label = result.match.title if result.match else result.destination.name
            episode = result.parsed.episode if result.parsed else None
            episode_label = (
                f" (S{episode.season:02d}E{episode.episode:02d})"
                if episode else ""
            )
            print(
                f"[TMDB] Clasificado: {label}{episode_label} "
                f"| archivo={dest.name}",
                flush=True,
            )
            self._progress.status(f"Clasificado: {label}")
            return result.destination

        print(
            f"[TMDB] Sin clasificar: {dest.name} | motivo={result.reason}",
            flush=True,
        )
        self._progress.status(f"Sin clasificar ({result.reason}): Inbox")
        return dest

    def _remove_existing_download(self, message: Message) -> None:
        existing = find_message_file(message, self._config.download_dir)
        if existing is not None:
            existing.unlink()
        self.downloaded_ids.discard(message.id)

    @staticmethod
    def _build_classifier(config: AppConfig) -> MediaClassifier | None:
        if not config.classification_enabled or config.tmdb_api_key is None:
            return None
        return build_media_classifier(
            api_key=config.tmdb_api_key,
            download_dir=config.download_dir,
            cache_file=config.tmdb_cache_file,
            language=config.tmdb_language,
            confidence_threshold=config.tmdb_confidence_threshold,
            ambiguity_delta=config.tmdb_ambiguity_delta,
        )
