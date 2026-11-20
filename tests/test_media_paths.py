"""Path correspondence and EXDEV-safe move tests for the single-mount media layout."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from telegram_downloader.media_classifier import (
    destination_for_kind,
    inbox_dir,
    library_search_roots,
    media_root_from_download_dir,
    move_without_overwrite,
    movies_dir,
    series_dir,
)


@pytest.fixture
def media_tree(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    for name in (
        "Downloads/Inbox",
        "Movies",
        "Series",
        "Documentaries",
        "Home Videos",
    ):
        (root / name).mkdir(parents=True)
    return root


def test_download_dir_maps_to_sibling_library_paths(media_tree: Path) -> None:
    download_dir = media_tree / "Downloads"

    assert media_root_from_download_dir(download_dir) == media_tree
    assert inbox_dir(download_dir) == media_tree / "Downloads" / "Inbox"
    assert movies_dir(download_dir) == media_tree / "Movies"
    assert series_dir(download_dir) == media_tree / "Series"
    assert destination_for_kind(download_dir, "movie") == media_tree / "Movies"
    assert destination_for_kind(download_dir, "series") == media_tree / "Series"

    # Movies/Series are siblings of Downloads — never under Downloads/Media/...
    assert "Downloads" not in movies_dir(download_dir).parts[-2:]
    assert movies_dir(download_dir).parent == download_dir.parent
    assert series_dir(download_dir).parent == download_dir.parent


def test_library_search_roots_include_inbox_movies_series(media_tree: Path) -> None:
    download_dir = media_tree / "Downloads"
    roots = library_search_roots(download_dir)
    assert inbox_dir(download_dir) in roots
    assert media_tree / "Movies" in roots
    assert media_tree / "Series" in roots


def test_nas_container_path_correspondence() -> None:
    """Documented container paths stay consistent with compose DOWNLOAD_DIR."""
    download_dir = Path("/data/media/Downloads")
    assert media_root_from_download_dir(download_dir) == Path("/data/media")
    assert inbox_dir(download_dir) == Path("/data/media/Downloads/Inbox")
    assert movies_dir(download_dir) == Path("/data/media/Movies")
    assert series_dir(download_dir) == Path("/data/media/Series")


def test_move_without_overwrite_same_filesystem(media_tree: Path) -> None:
    source = media_tree / "Downloads" / "Inbox" / "clip.mp4"
    source.write_bytes(b"video-bytes")
    dest = media_tree / "Movies" / "clip.mp4"

    result = move_without_overwrite(source, dest)

    assert result == dest
    assert dest.read_bytes() == b"video-bytes"
    assert not source.exists()


def test_move_without_overwrite_refuses_existing(media_tree: Path) -> None:
    source = media_tree / "Downloads" / "Inbox" / "clip.mp4"
    source.write_bytes(b"new")
    dest = media_tree / "Movies" / "clip.mp4"
    dest.write_bytes(b"old")

    with pytest.raises(FileExistsError):
        move_without_overwrite(source, dest)

    assert source.exists()
    assert dest.read_bytes() == b"old"


def test_move_exdev_fallback_preserves_source_on_copy_failure(
    media_tree: Path,
) -> None:
    source = media_tree / "Downloads" / "Inbox" / "clip.mp4"
    source.write_bytes(b"keep-me")
    dest = media_tree / "Movies" / "clip.mp4"

    def boom_replace(self: Path, target: Path) -> Path:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    with patch.object(Path, "replace", boom_replace):
        with patch(
            "telegram_downloader.media_classifier.shutil.copy2",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError, match="disk full"):
                move_without_overwrite(source, dest)

    assert source.exists()
    assert source.read_bytes() == b"keep-me"
    assert not dest.exists()
    partials = list((media_tree / "Movies").glob("*.partial"))
    assert partials == []


def test_move_exdev_fallback_succeeds_and_removes_source(media_tree: Path) -> None:
    source = media_tree / "Downloads" / "Inbox" / "clip.mp4"
    source.write_bytes(b"payload")
    dest = media_tree / "Series" / "clip.mp4"

    def boom_replace(self: Path, target: Path) -> Path:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    with patch.object(Path, "replace", boom_replace):
        result = move_without_overwrite(source, dest)

    assert result == dest
    assert dest.read_bytes() == b"payload"
    assert not source.exists()
