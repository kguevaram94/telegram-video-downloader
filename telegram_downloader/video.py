"""Detección de mensajes de video en Telegram."""

from telethon.tl.custom.message import Message
from telethon.tl.types import DocumentAttributeVideo


def is_video_message(message: Message) -> bool:
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
