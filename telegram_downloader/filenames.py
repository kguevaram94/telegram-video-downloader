"""Generación de nombres de archivo seguros para videos descargados."""

from __future__ import annotations

import re
from pathlib import Path

from telethon.tl.custom.message import Message
from telethon.tl.types import DocumentAttributeFilename

VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts"})

_MIME_TO_EXT = {
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
}

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')
_WHITESPACE = re.compile(r"\s+")


def safe_filename(name: str) -> str:
    cleaned = _WHITESPACE.sub(" ", name.strip()) or "video"
    cleaned = _INVALID_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(" .")
    return cleaned or "video"


def guess_extension(message: Message) -> str:
    if message.file and message.file.ext:
        ext = message.file.ext
        return ext if ext.startswith(".") else f".{ext}"

    if message.document and message.document.mime_type:
        return _MIME_TO_EXT.get(message.document.mime_type, ".mp4")

    return ".mp4"


def get_message_title(message: Message) -> str | None:
    text = (message.text or "").strip()
    if not text:
        return None
    return _WHITESPACE.sub(" ", text)


def _filename_from_title(message: Message, title: str) -> str:
    ext = guess_extension(message)
    name = safe_filename(title)
    if len(name) > 200:
        name = name[:200].rstrip(" .")

    if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
        name = f"{name}{ext}"

    return name


def _filename_from_document(message: Message) -> str | None:
    if message.file and message.file.name:
        return safe_filename(message.file.name)

    if not message.document:
        return None

    for attr in message.document.attributes or []:
        if isinstance(attr, DocumentAttributeFilename):
            return safe_filename(attr.file_name)

    return None


def get_original_filename(message: Message) -> str:
    title = get_message_title(message)
    if title:
        return _filename_from_title(message, title)

    from_document = _filename_from_document(message)
    if from_document:
        return from_document

    ext = guess_extension(message)
    date = message.date.strftime("%Y-%m-%d_%H%M%S")
    return f"video_{date}_{message.id}{ext}"


def month_dir_for_message(message: Message, download_dir: Path) -> Path:
    return download_dir / message.date.strftime("%Y-%m")


def base_target_path(message: Message, download_dir: Path) -> Path:
    return month_dir_for_message(message, download_dir) / get_original_filename(message)


def find_message_file(message: Message, download_dir: Path) -> Path | None:
    base = base_target_path(message, download_dir)
    if base.exists():
        return base

    alternate = base.parent / f"{base.stem}_{message.id}{base.suffix}"
    if alternate.exists():
        return alternate

    return None


def resolve_target_path(message: Message, download_dir: Path) -> Path:
    month_dir = month_dir_for_message(message, download_dir)
    month_dir.mkdir(parents=True, exist_ok=True)

    path = month_dir / get_original_filename(message)
    if path.exists():
        path = month_dir / f"{path.stem}_{message.id}{path.suffix}"

    return path
