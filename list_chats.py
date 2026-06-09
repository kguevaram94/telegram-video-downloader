#!/usr/bin/env python3
"""Lista tus chats de Telegram para encontrar el ID del grupo."""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / "config.env")

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", str(ROOT / "telegram_session"))


async def main() -> None:
    if not API_ID or not API_HASH:
        print("Crea config.env a partir de config.env.example y rellena API_ID y API_HASH.")
        sys.exit(1)

    client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
    await client.start()

    print("\nTus chats (busca el grupo donde reenvías los videos):\n")
    async for dialog in client.iter_dialogs():
        kind = "grupo" if dialog.is_group else "canal" if dialog.is_channel else "chat"
        print(f"{dialog.id:>15}  |  [{kind}]  {dialog.name}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
