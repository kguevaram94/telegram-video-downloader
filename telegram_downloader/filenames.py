"""Generación de nombres de archivo seguros para videos descargados."""

from __future__ import annotations

import re
from pathlib import Path

from telethon.tl.custom.message import Message
from telethon.tl.types import DocumentAttributeFilename

from telegram_downloader.media_classifier import inbox_dir, library_search_roots

VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts"})

_MIME_TO_EXT = {
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
}

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')
_WHITESPACE = re.compile(r"\s+")
_MONTH_DIR = re.compile(r"^\d{4}-\d{2}$")


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


def get_document_filename(message: Message) -> str | None:
    return _filename_from_document(message)


def month_dir_for_message(message: Message, download_dir: Path) -> Path:
    """Landing path under Downloads/Inbox (YYYY-MM), same mount as Movies/Series."""
    return inbox_dir(download_dir) / message.date.strftime("%Y-%m")


def base_target_path(message: Message, download_dir: Path) -> Path:
    return month_dir_for_message(message, download_dir) / get_original_filename(message)


def _candidate_names(message: Message) -> list[str]:
    filename = get_original_filename(message)
    stem_with_id = f"{Path(filename).stem}_{message.id}{Path(filename).suffix}"
    return [filename, stem_with_id]


def _candidate_paths(message: Message, root: Path) -> list[Path]:
    filename = get_original_filename(message)
    stem_with_id = f"{Path(filename).stem}_{message.id}{Path(filename).suffix}"
    month = message.date.strftime("%Y-%m")
    return [
        root / month / filename,
        root / month / stem_with_id,
        root / filename,
        root / stem_with_id,
    ]


def _looks_like_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def _find_nested_by_name(root: Path, names: list[str]) -> Path | None:
    if not root.is_dir():
        return None
    wanted = set(names)
    for path in root.rglob("*"):
        if path.name in wanted and _looks_like_video(path):
            return path
    return None


def _find_in_legacy_month_dirs(download_dir: Path, names: list[str]) -> Path | None:
    if not download_dir.is_dir():
        return None
    for child in download_dir.iterdir():
        if not child.is_dir() or not _MONTH_DIR.match(child.name):
            continue
        for name in names:
            candidate = child / name
            if _looks_like_video(candidate):
                return candidate
    return None


def find_message_file(message: Message, download_dir: Path) -> Path | None:
    """Find a downloaded file in Inbox, Movies, Series, or legacy paths."""
    names = _candidate_names(message)
    search_roots = list(library_search_roots(download_dir))
    search_roots.append(download_dir)

    seen: set[Path] = set()
    for root in search_roots:
        for candidate in _candidate_paths(message, root):
            if candidate in seen:
                continue
            seen.add(candidate)
            if _looks_like_video(candidate):
                return candidate

        nested = _find_nested_by_name(root, names)
        if nested is not None:
            return nested

    return _find_in_legacy_month_dirs(download_dir, names)


def resolve_target_path(message: Message, download_dir: Path) -> Path:
    month_dir = month_dir_for_message(message, download_dir)
    month_dir.mkdir(parents=True, exist_ok=True)

    path = month_dir / get_original_filename(message)
    if path.exists():
        path = month_dir / f"{path.stem}_{message.id}{path.suffix}"

    return path
