#!/usr/bin/env python3
"""Lista tus chats de Telegram para encontrar el ID del grupo."""

import asyncio

from telegram_downloader.list_chats_app import main

if __name__ == "__main__":
    asyncio.run(main())
