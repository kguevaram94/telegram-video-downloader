"""Path helpers and same-filesystem moves for the media library.

This module is intentionally limited to path layout + move utilities.
It does NOT implement TMDb API, cache, or classification thresholds —
those are absent from this repository's remotes/PRs.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

LIBRARY_SIBLINGS = (
    "Movies",
    "Series",
    "Documentaries",
    "Home Videos",
)


def media_root_from_download_dir(download_dir: Path) -> Path:
    """Return the media library root that contains Downloads as a sibling.

    DOWNLOAD_DIR is expected to be ``…/Downloads`` (e.g. ``/data/media/Downloads``).
    """
    resolved = download_dir.resolve()
    if resolved.name == "Downloads":
        return resolved.parent
    return resolved


def inbox_dir(download_dir: Path) -> Path:
    return download_dir / "Inbox"


def movies_dir(download_dir: Path) -> Path:
    return media_root_from_download_dir(download_dir) / "Movies"


def series_dir(download_dir: Path) -> Path:
    return media_root_from_download_dir(download_dir) / "Series"


def library_search_roots(download_dir: Path) -> tuple[Path, ...]:
    """Roots where an already-downloaded file may live (Inbox + library siblings)."""
    root = media_root_from_download_dir(download_dir)
    return (
        inbox_dir(download_dir),
        root / "Movies",
        root / "Series",
        root / "Documentaries",
        root / "Home Videos",
    )


def destination_for_kind(download_dir: Path, kind: str) -> Path:
    """Map a library kind to a sibling folder under the media root."""
    normalized = kind.strip().lower()
    mapping = {
        "movie": "Movies",
        "movies": "Movies",
        "series": "Series",
        "tv": "Series",
        "documentary": "Documentaries",
        "documentaries": "Documentaries",
        "home": "Home Videos",
        "home video": "Home Videos",
        "home videos": "Home Videos",
    }
    folder = mapping.get(normalized)
    if folder is None:
        raise ValueError(f"Unknown media kind: {kind!r}")
    return media_root_from_download_dir(download_dir) / folder


def move_without_overwrite(source: Path, destination: Path) -> Path:
    """Move ``source`` to ``destination`` without clobbering an existing file.

    Prefers ``Path.replace()`` (atomic rename on the same filesystem).
    On EXDEV (cross-device), falls back to copy+verify+unlink so a failed
    copy never deletes the original.
    """
    if not source.is_file():
        raise FileNotFoundError(f"Source file not found: {source}")

    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        source.replace(destination)
        return destination
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        return _copy_then_unlink(source, destination)


def _copy_then_unlink(source: Path, destination: Path) -> Path:
    """EXDEV fallback: copy, verify size, then remove source. Keep source on failure."""
    tmp = destination.with_name(destination.name + ".partial")
    try:
        if tmp.exists():
            tmp.unlink()
        shutil.copy2(source, tmp)
        source_size = source.stat().st_size
        copied_size = tmp.stat().st_size
        if copied_size != source_size:
            raise OSError(
                f"Copy size mismatch for {source}: {copied_size} != {source_size}"
            )
        os.replace(tmp, destination)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        # Original must still exist — do not unlink source on failure.
        if not source.exists():
            raise RuntimeError(
                f"Source missing after failed cross-device move: {source}"
            ) from None
        raise

    source.unlink()
    return destination
