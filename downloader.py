#!/usr/bin/env python3
"""Descarga automaticamente videos nuevos de un grupo de Telegram."""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / "config.env")

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
CHAT_ID = os.getenv("CHAT_ID")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(Path.home() / "Videos" / "Telegram")))
SESSION_NAME = os.getenv("SESSION_NAME", str(ROOT / "telegram_session"))
STATE_FILE = Path(os.getenv("STATE_FILE", str(ROOT / "downloaded.json")))
PROGRESS_FILE = Path(os.getenv("PROGRESS_FILE", str(ROOT / "downloader.progress")))
NOTIFY_ENABLED = os.getenv("NOTIFY", "true").lower() in {"1", "true", "yes", "on"}
NOTIFY_APP_NAME = os.getenv("NOTIFY_APP_NAME", "Telegram Downloader")


def require_config() -> tuple[int, str, int, Path]:
    missing = [name for name, value in [("API_ID", API_ID), ("API_HASH", API_HASH), ("CHAT_ID", CHAT_ID)] if not value]
    if missing:
        print(f"Faltan variables en config.env: {', '.join(missing)}")
        print("Copia config.env.example a config.env y completa los valores.")
        sys.exit(1)

    return int(API_ID), API_HASH, int(CHAT_ID), DOWNLOAD_DIR


def is_video_message(message) -> bool:
    if message.video:
        return True
    if not message.document:
        return False

    mime = message.document.mime_type or ""
    if mime.startswith("video/"):
        return True

    for attr in message.document.attributes or []:
        if isinstance(attr, DocumentAttributeVideo):
            return True

    return False


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts"}


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name.strip()) or "video"
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", cleaned)
    cleaned = cleaned.strip(" .")
    return cleaned or "video"


def guess_extension(message) -> str:
    if message.file and message.file.ext:
        ext = message.file.ext
        return ext if ext.startswith(".") else f".{ext}"

    if message.document and message.document.mime_type:
        ext_map = {
            "video/quicktime": ".mov",
            "video/x-matroska": ".mkv",
            "video/webm": ".webm",
        }
        return ext_map.get(message.document.mime_type, ".mp4")

    return ".mp4"


def get_message_title(message) -> Optional[str]:
    text = (message.text or "").strip()
    if not text:
        return None
    return re.sub(r"\s+", " ", text)


def get_original_filename(message) -> str:
    ext = guess_extension(message)
    title = get_message_title(message)

    if title:
        name = safe_filename(title)
        if len(name) > 200:
            name = name[:200].rstrip(" .")

        if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
            name = f"{name}{ext}"

        return name

    if message.file and message.file.name:
        return safe_filename(message.file.name)

    if message.document:
        for attr in message.document.attributes or []:
            if isinstance(attr, DocumentAttributeFilename):
                return safe_filename(attr.file_name)

    date = message.date.strftime("%Y-%m-%d_%H%M%S")
    return f"video_{date}_{message.id}{ext}"


def target_path(message) -> Path:
    month_dir = DOWNLOAD_DIR / message.date.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)

    path = month_dir / get_original_filename(message)
    if path.exists():
        path = month_dir / f"{path.stem}_{message.id}{path.suffix}"

    return path


def load_state() -> set[int]:
    if not STATE_FILE.exists():
        return set()

    return set(json.loads(STATE_FILE.read_text()))


def save_state(downloaded_ids: set[int]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(downloaded_ids)))


def format_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "tamaño desconocido"
    if num_bytes >= 1024 * 1024 * 1024:
        return f"{num_bytes / 1024 / 1024 / 1024:.2f} GB"
    return f"{num_bytes / 1024 / 1024:.1f} MB"


def escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def truncate_notification(text: str, max_len: int = 120) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def notify_macos(title: str, message: str) -> None:
    if not NOTIFY_ENABLED or sys.platform != "darwin":
        return

    safe_title = escape_applescript(truncate_notification(title, 80))
    safe_message = escape_applescript(truncate_notification(message))
    script = (
        f'display notification "{safe_message}" '
        f'with title "{safe_title}" '
        f'subtitle "{NOTIFY_APP_NAME}"'
    )

    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return


async def notify_async(title: str, message: str) -> None:
    await asyncio.to_thread(notify_macos, title, message)


def clear_progress_file() -> None:
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()


def write_progress_file(text: str) -> None:
    PROGRESS_FILE.write_text(text, encoding="utf-8")


class ProgressReporter:
    def __init__(self) -> None:
        self._interactive = sys.stdout.isatty()
        self._last_line_len = 0
        self._last_pct = -1

    def status(self, text: str) -> None:
        if self._interactive:
            padded = text.ljust(max(len(text), self._last_line_len))
            sys.stdout.write(f"\r{padded}")
            sys.stdout.flush()
            self._last_line_len = len(padded)
            return

        write_progress_file(text)

    def finish(self, text: str) -> None:
        if self._interactive:
            sys.stdout.write("\r" + " " * self._last_line_len + "\r")
            sys.stdout.flush()
            self._last_line_len = 0
            self._last_pct = -1
        else:
            clear_progress_file()

        print(text, flush=True)

    def make_callback(self, label: str, queue_waiting: Callable[[], int]):
        def progress(current: int, total: int) -> None:
            waiting = queue_waiting()
            cola = f"[+{waiting} en cola] " if waiting else ""

            if total:
                pct = int(current * 100 / total)
                if self._interactive and pct == self._last_pct:
                    return
                self._last_pct = pct
                self.status(
                    f"{cola}{label}: {pct:3d}% ({format_size(current)} / {format_size(total)})"
                )
                return

            if not self._interactive:
                if current and current % (10 * 1024 * 1024) < 512 * 1024:
                    write_progress_file(f"{cola}{label}: {format_size(current)} descargados...")
                return

            self.status(f"{cola}{label}: {format_size(current)} descargados...")

        return progress


class DownloadManager:
    def __init__(self, downloaded_ids: set[int]) -> None:
        self.downloaded_ids = downloaded_ids
        self.queue: asyncio.Queue = asyncio.Queue()
        self.queued_ids: set[int] = set()
        self.in_progress_ids: set[int] = set()
        self.progress = ProgressReporter()

    def queue_waiting(self) -> int:
        return self.queue.qsize()

    async def enqueue(self, client: TelegramClient, message) -> None:
        if message.id in self.downloaded_ids:
            return
        if message.id in self.queued_ids or message.id in self.in_progress_ids:
            return

        self.queued_ids.add(message.id)
        await self.queue.put((client, message))

        waiting = self.queue.qsize()
        dest = target_path(message)
        total_size = message.file.size if message.file else None
        queue_note = f" (+{waiting} en cola)" if waiting else ""
        await notify_async(
            "Video detectado",
            f"{dest.name} ({format_size(total_size)}){queue_note}",
        )

    async def worker(self) -> None:
        while True:
            client, message = await self.queue.get()
            self.queued_ids.discard(message.id)
            self.in_progress_ids.add(message.id)

            try:
                await self._download_one(client, message)
            except Exception as exc:
                clear_progress_file()
                self.progress.finish(f"Error mensaje {message.id}: {exc}")
                await notify_async("Error de descarga", f"Mensaje {message.id}: {exc}")
            finally:
                self.in_progress_ids.discard(message.id)
                self.queue.task_done()

    async def _download_one(self, client: TelegramClient, message) -> None:
        if message.id in self.downloaded_ids:
            return

        dest = target_path(message)
        total_size = message.file.size if message.file else None
        temp_dest = dest.with_suffix(dest.suffix + ".part")
        if temp_dest.exists():
            temp_dest.unlink()

        self.progress._last_pct = -1
        cola = f"[+{self.queue_waiting()} en cola] " if self.queue_waiting() else ""
        total_label = format_size(total_size)
        self.progress.status(f"{cola}{dest.name}:   0% (0 MB / {total_label})")

        await client.download_media(
            message,
            file=str(temp_dest),
            progress_callback=self.progress.make_callback(dest.name, self.queue_waiting),
        )
        temp_dest.replace(dest)

        self.downloaded_ids.add(message.id)
        save_state(self.downloaded_ids)
        self.progress.finish(f"Completado: {dest.name} ({format_size(dest.stat().st_size)})")
        await notify_async(
            "Descarga completada",
            f"{dest.name} ({format_size(dest.stat().st_size)})",
        )

    async def backfill(self, client: TelegramClient, chat_id: int, limit: int) -> None:
        print(f"Buscando hasta {limit} mensajes recientes con video...", flush=True)
        found = 0

        async for message in client.iter_messages(chat_id, limit=limit):
            if not is_video_message(message):
                continue
            await self.enqueue(client, message)
            found += 1

        if found:
            await self.queue.join()
            print(f"Backfill completado: {found} video(s) procesado(s).", flush=True)
            return

        print("Backfill completado: no habia videos recientes.", flush=True)



async def main() -> None:
    parser = argparse.ArgumentParser(description="Descarga videos de un grupo de Telegram.")
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="N",
        help="Descarga videos de los ultimos N mensajes al iniciar (opcional).",
    )
    args = parser.parse_args()

    api_id, api_hash, chat_id, download_dir = require_config()
    download_dir.mkdir(parents=True, exist_ok=True)
    downloaded_ids = load_state()
    manager = DownloadManager(downloaded_ids)

    client = TelegramClient(SESSION_NAME, api_id, api_hash, device_model=NOTIFY_APP_NAME)
    await client.start()
    worker_task = asyncio.create_task(manager.worker())

    try:
        entity = await client.get_entity(chat_id)
        title = getattr(entity, "title", str(chat_id))
    except Exception as exc:
        worker_task.cancel()
        print(f"No se pudo acceder al chat {chat_id}: {exc}")
        print("Verifica CHAT_ID con list_chats.py")
        await client.disconnect()
        sys.exit(1)

    print(f"Monitoreando: {title}")
    print(f"Carpeta destino: {download_dir}")

    if args.backfill:
        await manager.backfill(client, chat_id, args.backfill)

    @client.on(events.NewMessage(chats=chat_id))
    async def on_new_message(event) -> None:
        message = event.message
        if not is_video_message(message):
            return

        await manager.enqueue(client, message)

    print("Esperando nuevos videos... (Ctrl+C para salir)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
