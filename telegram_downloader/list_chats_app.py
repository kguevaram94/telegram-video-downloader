"""Lista chats de Telegram para obtener el ID del grupo."""

from __future__ import annotations

import asyncio

from telethon import TelegramClient

from telegram_downloader.config import load_config


def _dialog_kind(is_group: bool, is_channel: bool) -> str:
    if is_group:
        return "grupo"
    if is_channel:
        return "canal"
    return "chat"


async def main() -> None:
    config = load_config()
    client = TelegramClient(
        config.session_name,
        config.api_id,
        config.api_hash,
        device_model=config.notify_app_name,
    )
    await client.start()

    print("\nTus chats (busca el grupo donde reenvías los videos):\n")
    async for dialog in client.iter_dialogs():
        kind = _dialog_kind(dialog.is_group, dialog.is_channel)
        print(f"{dialog.id:>15}  |  [{kind}]  {dialog.name}")

    await client.disconnect()
