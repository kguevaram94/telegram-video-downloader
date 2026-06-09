"""Punto de entrada del monitor y descargador de videos."""

from __future__ import annotations

import argparse
import asyncio
import sys

from telethon import TelegramClient, events

from telegram_downloader.config import AppConfig, load_config
from telegram_downloader.download_manager import DownloadManager
from telegram_downloader.state import load_downloaded_ids
from telegram_downloader.video import is_video_message


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Descarga videos de un grupo de Telegram.",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="N",
        help="Descarga videos de los ultimos N mensajes al iniciar (opcional).",
    )
    return parser.parse_args()


async def _resolve_chat_title(client: TelegramClient, chat_id: int) -> str:
    entity = await client.get_entity(chat_id)
    return getattr(entity, "title", str(chat_id))


async def _run_monitor(
    client: TelegramClient,
    config: AppConfig,
    manager: DownloadManager,
    *,
    backfill_limit: int | None,
) -> None:
    chat_id = config.chat_id
    if chat_id is None:
        raise RuntimeError("CHAT_ID no configurado")

    try:
        title = await _resolve_chat_title(client, chat_id)
    except Exception as exc:
        print(f"No se pudo acceder al chat {chat_id}: {exc}")
        print("Verifica CHAT_ID con list_chats.py")
        raise

    print(f"Monitoreando: {title}")
    print(f"Carpeta destino: {config.download_dir}")

    if backfill_limit:
        await manager.backfill(client, chat_id, backfill_limit)

    @client.on(events.NewMessage(chats=chat_id))
    async def on_new_message(event: events.NewMessage.Event) -> None:
        message = event.message
        if not is_video_message(message):
            return
        await manager.enqueue(client, message)

    print("Esperando nuevos videos... (Ctrl+C para salir)")
    await client.run_until_disconnected()


async def main() -> None:
    args = _parse_args()
    config = load_config(require_chat_id=True)
    config.download_dir.mkdir(parents=True, exist_ok=True)

    downloaded_ids = load_downloaded_ids(config.state_file)
    manager = DownloadManager(config, downloaded_ids)

    client = TelegramClient(
        config.session_name,
        config.api_id,
        config.api_hash,
        device_model=config.notify_app_name,
    )
    await client.start()
    worker_task = asyncio.create_task(manager.worker())

    try:
        await _run_monitor(
            client,
            config,
            manager,
            backfill_limit=args.backfill,
        )
    except Exception:
        worker_task.cancel()
        await client.disconnect()
        sys.exit(1)
