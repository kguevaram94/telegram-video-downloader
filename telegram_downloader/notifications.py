"""Notificaciones nativas de macOS."""

from __future__ import annotations

import asyncio
import subprocess
import sys


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def notify_macos(
    title: str,
    message: str,
    *,
    enabled: bool,
    app_name: str,
) -> None:
    if not enabled or sys.platform != "darwin":
        return

    safe_title = _escape_applescript(_truncate(title, 80))
    safe_message = _escape_applescript(_truncate(message, 120))
    script = (
        f'display notification "{safe_message}" '
        f'with title "{safe_title}" '
        f'subtitle "{app_name}"'
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


async def notify_async(
    title: str,
    message: str,
    *,
    enabled: bool,
    app_name: str,
) -> None:
    await asyncio.to_thread(
        notify_macos,
        title,
        message,
        enabled=enabled,
        app_name=app_name,
    )
