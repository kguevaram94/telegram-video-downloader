#!/usr/bin/env python3
"""Descarga automaticamente videos nuevos de un grupo de Telegram."""

import asyncio

from telegram_downloader.downloader_app import main

if __name__ == "__main__":
    asyncio.run(main())
