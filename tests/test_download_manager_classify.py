"""Integración de clasificación post-descarga en DownloadManager."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_downloader.config import AppConfig
from telegram_downloader.download_manager import DownloadManager
from telegram_downloader.media_classifier import ClassificationResult, TmdbMatch


def _config(tmp_path: Path, *, enabled: bool = True) -> AppConfig:
    download_dir = tmp_path / "media" / "Downloads"
    download_dir.mkdir(parents=True)
    (tmp_path / "media" / "Movies").mkdir(parents=True)
    (tmp_path / "media" / "Series").mkdir(parents=True)
    return AppConfig(
        api_id=1,
        api_hash="hash",
        chat_id=-100,
        download_dir=download_dir,
        session_name=str(tmp_path / "session"),
        state_file=tmp_path / "downloaded.json",
        progress_file=tmp_path / "progress",
        manual_queue_file=tmp_path / "queue.json",
        notify_enabled=False,
        notify_app_name="test",
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        log_path=tmp_path / "log",
        static_dir=tmp_path,
        tmdb_api_key="test-key" if enabled else None,
        tmdb_enabled=enabled,
        tmdb_confidence_threshold=0.72,
        tmdb_ambiguity_delta=0.08,
        tmdb_language="es-ES",
        tmdb_cache_file=tmp_path / "tmdb_cache.json",
    )


def _message(*, message_id: int = 7, title: str = "Inception 2010") -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        text=title,
        date=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
        file=SimpleNamespace(ext=".mp4", name="Inception.2010.mkv", size=12),
        document=None,
        video=True,
    )


def test_classify_runs_only_after_part_rename(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config(tmp_path)
        manager = DownloadManager(config, set())
        message = _message()
        client = MagicMock()
        order: list[str] = []

        async def fake_download_media(
            _message: Any,
            *,
            file: str,
            progress_callback: Any = None,
        ) -> None:
            order.append("download")
            Path(file).write_bytes(b"part-bytes")

        client.download_media = AsyncMock(side_effect=fake_download_media)

        async def fake_classify(dest: Path, _message: Any) -> Path:
            order.append("classify")
            assert dest.exists()
            assert dest.suffix == ".mp4"
            assert not dest.with_suffix(dest.suffix + ".part").exists()
            return dest

        with (
            patch.object(manager, "_classify_downloaded_file", side_effect=fake_classify),
            patch(
                "telegram_downloader.download_manager.notify_async",
                new=AsyncMock(),
            ),
            patch("telegram_downloader.download_manager.save_downloaded_ids"),
        ):
            await manager._download_one(client, message)

        assert order == ["download", "classify"]
        assert message.id in manager.downloaded_ids

    asyncio.run(scenario())


def test_tmdb_failure_keeps_inbox_and_marks_downloaded(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config(tmp_path)
        manager = DownloadManager(config, set())
        message = _message()
        client = MagicMock()

        async def fake_download_media(
            _message: Any,
            *,
            file: str,
            progress_callback: Any = None,
        ) -> None:
            Path(file).write_bytes(b"video")

        client.download_media = AsyncMock(side_effect=fake_download_media)

        classifier = MagicMock()
        classifier.classify_and_relocate = AsyncMock(
            return_value=ClassificationResult("inbox", "error_tmdb:RuntimeError")
        )
        manager._classifier = classifier

        with (
            patch(
                "telegram_downloader.download_manager.notify_async",
                new=AsyncMock(),
            ),
            patch("telegram_downloader.download_manager.save_downloaded_ids"),
        ):
            await manager._download_one(client, message)

        inbox_file = config.download_dir / "Inbox" / "2026-11" / "Inception 2010.mp4"
        assert inbox_file.exists()
        assert message.id in manager.downloaded_ids
        classifier.classify_and_relocate.assert_awaited_once()

    asyncio.run(scenario())


def test_successful_classification_moves_out_of_downloads(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config(tmp_path)
        manager = DownloadManager(config, set())
        message = _message()
        client = MagicMock()

        async def fake_download_media(
            _message: Any,
            *,
            file: str,
            progress_callback: Any = None,
        ) -> None:
            Path(file).write_bytes(b"video")

        client.download_media = AsyncMock(side_effect=fake_download_media)

        final = (
            tmp_path / "media" / "Movies" / "Inception (2010)" / "Inception 2010.mp4"
        )
        final.parent.mkdir(parents=True)
        final.write_bytes(b"video")

        classifier = MagicMock()
        classifier.classify_and_relocate = AsyncMock(
            return_value=ClassificationResult(
                "classified",
                "ok",
                match=TmdbMatch(
                    media_type="movie",
                    tmdb_id=1,
                    title="Inception",
                    year=2010,
                    score=0.95,
                ),
                destination=final,
            )
        )
        manager._classifier = classifier

        with (
            patch(
                "telegram_downloader.download_manager.notify_async",
                new=AsyncMock(),
            ),
            patch("telegram_downloader.download_manager.save_downloaded_ids"),
        ):
            await manager._download_one(client, message)

        assert final.parent.parent.name == "Movies"
        assert message.id in manager.downloaded_ids

    asyncio.run(scenario())
