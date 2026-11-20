"""Inbox landing path and sibling library search for filenames helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telegram_downloader.filenames import (
    find_message_file,
    month_dir_for_message,
    resolve_target_path,
)


def _fake_message(*, message_id: int = 42, title: str = "Test Episode") -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        text=title,
        date=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc),
        file=SimpleNamespace(ext=".mp4", name=None, size=10),
        document=None,
    )


def test_resolve_target_path_lands_in_inbox(tmp_path: Path) -> None:
    download_dir = tmp_path / "Downloads"
    download_dir.mkdir()
    message = _fake_message()

    dest = resolve_target_path(message, download_dir)

    assert dest.parent == download_dir / "Inbox" / "2026-11"
    assert dest.name == "Test Episode.mp4"
    assert dest.parent.is_dir()


def test_month_dir_is_under_inbox(tmp_path: Path) -> None:
    download_dir = tmp_path / "Downloads"
    message = _fake_message()
    assert month_dir_for_message(message, download_dir) == (
        download_dir / "Inbox" / "2026-11"
    )


def test_find_message_file_in_inbox(tmp_path: Path) -> None:
    download_dir = tmp_path / "Downloads"
    inbox_month = download_dir / "Inbox" / "2026-11"
    inbox_month.mkdir(parents=True)
    path = inbox_month / "Test Episode.mp4"
    path.write_bytes(b"x")

    found = find_message_file(_fake_message(), download_dir)
    assert found == path


def test_find_message_file_in_movies_sibling(tmp_path: Path) -> None:
    media = tmp_path / "media"
    download_dir = media / "Downloads"
    movies = media / "Movies"
    movies.mkdir(parents=True)
    download_dir.mkdir()
    path = movies / "Test Episode.mp4"
    path.write_bytes(b"x")

    found = find_message_file(_fake_message(), download_dir)
    assert found == path


def test_find_message_file_in_series_sibling(tmp_path: Path) -> None:
    media = tmp_path / "media"
    download_dir = media / "Downloads"
    series = media / "Series"
    series.mkdir(parents=True)
    download_dir.mkdir()
    path = series / "Test Episode.mp4"
    path.write_bytes(b"x")

    found = find_message_file(_fake_message(), download_dir)
    assert found == path


def test_find_message_file_nested_movie_folder(tmp_path: Path) -> None:
    media = tmp_path / "media"
    download_dir = media / "Downloads"
    download_dir.mkdir(parents=True)
    nested = media / "Movies" / "Test Episode (2020)" / "Test Episode.mp4"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")

    found = find_message_file(_fake_message(), download_dir)
    assert found == nested


def test_find_message_file_nested_series_season(tmp_path: Path) -> None:
    media = tmp_path / "media"
    download_dir = media / "Downloads"
    download_dir.mkdir(parents=True)
    nested = media / "Series" / "Test Episode" / "Season 01" / "Test Episode.mp4"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x")

    found = find_message_file(_fake_message(), download_dir)
    assert found == nested


def test_find_message_file_legacy_month_dir(tmp_path: Path) -> None:
    download_dir = tmp_path / "Downloads"
    legacy = download_dir / "2026-06" / "Test Episode.mp4"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"x")

    found = find_message_file(_fake_message(), download_dir)
    assert found == legacy
