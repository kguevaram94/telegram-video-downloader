"""Integración de clasificación post-descarga en DownloadManager."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_downloader.config import AppConfig
from telegram_downloader.download_manager import AutoClassifyOutcome, DownloadManager
from telegram_downloader.media_classifier import ClassificationResult, TmdbMatch
from telegram_downloader.state import load_downloaded_ids


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

        async def fake_classify(dest: Path, _message: Any) -> AutoClassifyOutcome:
            order.append("classify")
            assert dest.exists()
            assert dest.suffix == ".mp4"
            assert not dest.with_suffix(dest.suffix + ".part").exists()
            return AutoClassifyOutcome(path=dest, status="executed")

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


def _manager_with_event_loop(tmp_path: Path) -> DownloadManager:
    """DownloadManager crea asyncio.Queue; hace falta un loop en Python 3.9."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return DownloadManager(_config(tmp_path), set())


def test_successful_classification_is_logged(tmp_path: Path) -> None:
    from contextlib import redirect_stdout
    from io import StringIO

    manager = _manager_with_event_loop(tmp_path)
    dest = manager._config.download_dir / "Inbox" / "Wayward Pines S02E09.mkv"
    destination = (
        manager._config.download_dir.parent
        / "Series"
        / "Wayward Pines"
        / "Season 02"
        / dest.name
    )

    result = ClassificationResult(
        "classified",
        "ok",
        match=TmdbMatch(
            media_type="tv",
            tmdb_id=53425,
            title="Wayward Pines",
            year=None,
            score=0.95,
        ),
        parsed=SimpleNamespace(
            episode=SimpleNamespace(season=2, episode=9)
        ),
        destination=destination,
    )

    output = StringIO()
    with redirect_stdout(output):
        manager._path_after_classification(dest, result)

    assert "[TMDB] Clasificado: Wayward Pines (S02E09)" in output.getvalue()


def test_unclassified_result_is_logged(tmp_path: Path) -> None:
    from contextlib import redirect_stdout
    from io import StringIO

    manager = _manager_with_event_loop(tmp_path)
    dest = manager._config.download_dir / "Inbox" / "unknown.mkv"

    result = ClassificationResult("inbox", "sin_resultados")

    output = StringIO()
    with redirect_stdout(output):
        returned = manager._path_after_classification(dest, result)

    assert returned == dest
    assert "[TMDB] Sin clasificar: unknown.mkv | motivo=sin_resultados" in output.getvalue()


def test_classification_exception_is_logged(tmp_path: Path) -> None:
    from contextlib import redirect_stdout
    from io import StringIO

    async def scenario() -> tuple[Path, str]:
        manager = DownloadManager(_config(tmp_path), set())
        dest = manager._config.download_dir / "Inbox" / "video.mkv"
        classifier = MagicMock()
        classifier.classify_and_relocate = AsyncMock(
            side_effect=RuntimeError("TMDB unavailable")
        )
        manager._classifier = classifier

        output = StringIO()
        with redirect_stdout(output):
            returned = await manager._classify_downloaded_file(dest, _message())
        return returned, output.getvalue()

    returned, logged = asyncio.run(scenario())
    assert returned.path.name == "video.mkv"
    assert returned.status == "executed"
    assert "[TMDB] Clasificación omitida: video.mkv | error=RuntimeError" in logged


def test_ambiguous_classification_marks_downloaded(tmp_path: Path) -> None:
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
            return_value=ClassificationResult("inbox", "ambiguo")
        )
        manager._classifier = classifier

        with (
            patch(
                "telegram_downloader.download_manager.notify_async",
                new=AsyncMock(),
            ),
        ):
            await manager._download_one(client, message)

        inbox_file = config.download_dir / "Inbox" / "2026-11" / "Inception 2010.mp4"
        assert inbox_file.exists()
        assert message.id in manager.downloaded_ids
        assert message.id in load_downloaded_ids(config.state_file)
        classifier.classify_and_relocate.assert_awaited_once()

    asyncio.run(scenario())


def test_classification_disabled_marks_downloaded(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config(tmp_path, enabled=False)
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
        assert manager._classifier is None

        with (
            patch(
                "telegram_downloader.download_manager.notify_async",
                new=AsyncMock(),
            ),
        ):
            await manager._download_one(client, message)

        inbox_file = config.download_dir / "Inbox" / "2026-11" / "Inception 2010.mp4"
        assert inbox_file.exists()
        assert message.id in manager.downloaded_ids
        assert message.id in load_downloaded_ids(config.state_file)

    asyncio.run(scenario())


def test_deferred_classification_does_not_mark_downloaded(tmp_path: Path) -> None:
    async def scenario() -> None:
        from telegram_downloader.classification_lock import ClassificationPathLock

        config = _config(tmp_path)
        shared = ClassificationPathLock()
        manager = DownloadManager(config, set(), classification_lock=shared)
        message = _message()
        client = MagicMock()
        payload = b"precious-deferred-bytes"

        async def fake_download_media(
            _message: Any,
            *,
            file: str,
            progress_callback: Any = None,
        ) -> None:
            Path(file).write_bytes(payload)

        client.download_media = AsyncMock(side_effect=fake_download_media)

        classifier = MagicMock()
        classifier.classify_and_relocate = AsyncMock(
            return_value=ClassificationResult("classified", "ok")
        )
        manager._classifier = classifier

        inbox_file = config.download_dir / "Inbox" / "2026-11" / "Inception 2010.mp4"
        assert shared.try_acquire(inbox_file)

        with (
            patch(
                "telegram_downloader.download_manager.notify_async",
                new=AsyncMock(),
            ),
        ):
            await manager._download_one(client, message)

        shared.release(inbox_file)

        assert inbox_file.exists()
        assert inbox_file.read_bytes() == payload
        assert message.id not in manager.downloaded_ids
        assert not config.state_file.exists()
        classifier.classify_and_relocate.assert_not_awaited()

    asyncio.run(scenario())


def test_tmdb_error_marks_downloaded_and_keeps_inbox_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = _config(tmp_path)
        manager = DownloadManager(config, set())
        message = _message()
        client = MagicMock()
        payload = b"inbox-kept-on-tmdb-error"

        async def fake_download_media(
            _message: Any,
            *,
            file: str,
            progress_callback: Any = None,
        ) -> None:
            Path(file).write_bytes(payload)

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
        ):
            await manager._download_one(client, message)

        inbox_file = config.download_dir / "Inbox" / "2026-11" / "Inception 2010.mp4"
        assert inbox_file.exists()
        assert inbox_file.read_bytes() == payload
        assert message.id in manager.downloaded_ids
        assert message.id in load_downloaded_ids(config.state_file)

    asyncio.run(scenario())
