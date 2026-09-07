"""Métricas ligeras para las cards del panel."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from telegram_downloader.formatting import format_size
from telegram_downloader.state import load_downloaded_ids

_DIR_SIZE_TTL_SECONDS = 30.0
_dir_size_cache: tuple[str, float, int | None] | None = None


def get_downloaded_count(state_file: Path) -> int:
    try:
        return len(load_downloaded_ids(state_file))
    except RuntimeError:
        return 0


def get_directory_size_bytes(path: Path) -> int | None:
    global _dir_size_cache
    now = time.monotonic()
    cache_key = str(path)
    if _dir_size_cache is not None:
        cached_key, cached_at, cached_size = _dir_size_cache
        if cached_key == cache_key and now - cached_at < _DIR_SIZE_TTL_SECONDS:
            return cached_size

    size = _measure_directory_size_bytes(path)
    if size is not None:
        _dir_size_cache = (cache_key, now, size)
    return size


def _measure_directory_size_bytes(path: Path) -> int | None:
    try:
        result = subprocess.run(
            ["/usr/bin/du", "-sk", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_du_kilobytes(result.stdout)


def _parse_du_kilobytes(output: str) -> int | None:
    first = output.strip().split(maxsplit=1)
    if not first:
        return None
    try:
        return int(first[0]) * 1024
    except ValueError:
        return None


def format_directory_size(num_bytes: int | None) -> str | None:
    if num_bytes is None:
        return None
    if num_bytes == 0:
        return "0 B"
    return format_size(num_bytes)
