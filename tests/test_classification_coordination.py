"""Coordinación del bloqueo entre clasificación automática y manual."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_downloader.classification_lock import (
    ClassificationPathLock,
    canonical_path_key,
    get_classification_lock,
)
from telegram_downloader.config import AppConfig
from telegram_downloader.dashboard.inbox_service import (
    InboxReclassifyService,
    reclassify_inbox_file,
)
from telegram_downloader.download_manager import AutoClassifyOutcome, DownloadManager
from telegram_downloader.media_classifier import (
    ClassificationResult,
    ParsedMediaName,
    TmdbMatch,
)
from telegram_downloader.state import load_downloaded_ids


def _media_layout(download_dir: Path) -> Path:
    download_dir.mkdir(parents=True, exist_ok=True)
    (download_dir / "Inbox").mkdir(parents=True, exist_ok=True)
    media_root = download_dir.parent
    (media_root / "Movies").mkdir(parents=True, exist_ok=True)
    (media_root / "Series").mkdir(parents=True, exist_ok=True)
    return download_dir


def _config(tmp: Path, download_dir: Path) -> AppConfig:
    return AppConfig(
        api_id=1,
        api_hash="hash",
        chat_id=-100,
        download_dir=download_dir,
        session_name=str(tmp / "session"),
        state_file=tmp / "downloaded.json",
        progress_file=tmp / "progress",
        manual_queue_file=tmp / "queue.json",
        notify_enabled=False,
        notify_app_name="test",
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        log_path=tmp / "log",
        static_dir=tmp,
        tmdb_api_key="test-key",
        tmdb_enabled=True,
        tmdb_confidence_threshold=0.72,
        tmdb_ambiguity_delta=0.08,
        tmdb_language="es-ES",
        tmdb_cache_file=tmp / "tmdb_cache.json",
    )


def _message(*, message_id: int = 7, title: str = "Inception 2010") -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        text=title,
        date=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
        file=SimpleNamespace(ext=".mkv", name="Inception 2010.mkv", size=12),
        document=None,
        video=True,
    )


def _classified_result(source: Path) -> ClassificationResult:
    return ClassificationResult(
        "classified",
        "ok",
        match=TmdbMatch(
            media_type="movie",
            tmdb_id=1,
            title="Inception",
            year=2010,
            score=0.99,
        ),
        parsed=ParsedMediaName(
            raw=source.name,
            title_query="Inception",
            year=2010,
            episode=None,
            content_type="movie",
            parser="test",
            search_queries=("Inception",),
            absolute_episode=None,
        ),
        destination=source,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class ClassificationPathLockTests(unittest.TestCase):
    def test_canonical_key_normalizes_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "media" / "Inbox"
            base.mkdir(parents=True)
            file_path = base / "a.mkv"
            file_path.write_bytes(b"x")
            relative = Path(tmp) / "media" / "Inbox" / ".." / "Inbox" / "a.mkv"
            self.assertEqual(
                canonical_path_key(file_path),
                canonical_path_key(relative),
            )

    def test_second_acquire_fails_until_release(self) -> None:
        lock = ClassificationPathLock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mkv"
            path.write_bytes(b"x")
            self.assertTrue(lock.try_acquire(path))
            self.assertTrue(lock.is_held(path))
            self.assertFalse(lock.try_acquire(path))
            lock.release(path)
            self.assertFalse(lock.is_held(path))
            self.assertTrue(lock.try_acquire(path))
            lock.release(path)

    def test_release_is_idempotent(self) -> None:
        lock = ClassificationPathLock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "video.mkv"
            path.write_bytes(b"x")
            lock.release(path)
            self.assertTrue(lock.try_acquire(path))
            lock.release(path)
            lock.release(path)
            self.assertTrue(lock.try_acquire(path))
            lock.release(path)


class ClassificationCoordinationTests(unittest.TestCase):
    def test_auto_active_blocks_manual_reclassify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            service = InboxReclassifyService(path_lock=shared)
            manager = DownloadManager(
                config,
                set(),
                classification_lock=shared,
            )

            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_classify_and_relocate(
                dest: Path,
                **_: Any,
            ) -> ClassificationResult:
                started.set()
                await release.wait()
                return _classified_result(dest)

            classifier = MagicMock()
            classifier.classify_and_relocate = AsyncMock(
                side_effect=slow_classify_and_relocate
            )
            classifier.classify_local_file = AsyncMock(
                return_value=_classified_result(source)
            )
            manager._classifier = classifier

            async def run() -> Any:
                auto_task = asyncio.create_task(
                    manager._classify_downloaded_file(source, _message())
                )
                await started.wait()
                manual = await reclassify_inbox_file(
                    config,
                    "Inception 2010.mkv",
                    classifier=classifier,
                    service=service,
                )
                release.set()
                auto_outcome = await auto_task
                return manual, auto_outcome

            manual, auto_outcome = _run(run())
            self.assertEqual(manual.status, "already_processing")
            self.assertEqual(manual.http_status, 409)
            self.assertEqual(auto_outcome.path, source)
            self.assertEqual(auto_outcome.status, "executed")
            self.assertFalse(shared.is_held(source))
            classifier.classify_local_file.assert_not_awaited()

    def test_manual_active_defers_auto_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            service = InboxReclassifyService(path_lock=shared)
            manager = DownloadManager(
                config,
                set(),
                classification_lock=shared,
            )

            started = asyncio.Event()
            release = asyncio.Event()

            class SlowClassifier:
                async def classify_local_file(
                    self,
                    source_path: Path,
                    **_: Any,
                ) -> ClassificationResult:
                    started.set()
                    await release.wait()
                    return _classified_result(source_path)

            auto_classifier = MagicMock()
            auto_classifier.classify_and_relocate = AsyncMock(
                return_value=_classified_result(source)
            )
            manager._classifier = auto_classifier

            async def run() -> tuple[Any, AutoClassifyOutcome]:
                manual_task = asyncio.create_task(
                    reclassify_inbox_file(
                        config,
                        "Inception 2010.mkv",
                        classifier=SlowClassifier(),  # type: ignore[arg-type]
                        service=service,
                    )
                )
                await started.wait()
                auto_outcome = await manager._classify_downloaded_file(
                    source,
                    _message(),
                )
                release.set()
                manual = await manual_task
                return manual, auto_outcome

            manual, auto_outcome = _run(run())
            self.assertEqual(manual.status, "classified")
            self.assertEqual(auto_outcome.path, source)
            self.assertEqual(auto_outcome.status, "deferred")
            self.assertTrue(source.exists())
            auto_classifier.classify_and_relocate.assert_not_awaited()
            self.assertFalse(shared.is_held(source))

    def test_lock_released_when_tmdb_fails_on_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            manager = DownloadManager(
                config,
                set(),
                classification_lock=shared,
            )
            classifier = MagicMock()
            classifier.classify_and_relocate = AsyncMock(
                side_effect=RuntimeError("TMDB unavailable")
            )
            manager._classifier = classifier

            returned = _run(
                manager._classify_downloaded_file(source, _message())
            )
            self.assertEqual(returned.path, source)
            self.assertEqual(returned.status, "executed")
            self.assertTrue(source.exists())
            self.assertFalse(shared.is_held(source))
            self.assertTrue(shared.try_acquire(source))
            shared.release(source)

    def test_lock_released_when_tmdb_fails_on_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            service = InboxReclassifyService(path_lock=shared)

            classifier = MagicMock()
            classifier.classify_local_file = AsyncMock(
                return_value=ClassificationResult(
                    "inbox",
                    "error_tmdb:RuntimeError",
                )
            )

            result = _run(
                reclassify_inbox_file(
                    config,
                    "Inception 2010.mkv",
                    classifier=classifier,
                    service=service,
                )
            )
            self.assertEqual(result.status, "tmdb_error")
            self.assertTrue(source.exists())
            self.assertFalse(shared.is_held(source))
            self.assertTrue(shared.try_acquire(source))
            shared.release(source)

    def test_lock_released_when_move_fails_on_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            manager = DownloadManager(
                config,
                set(),
                classification_lock=shared,
            )
            classifier = MagicMock()
            classifier.classify_and_relocate = AsyncMock(
                return_value=ClassificationResult("inbox", "error_move:OSError")
            )
            manager._classifier = classifier

            returned = _run(
                manager._classify_downloaded_file(source, _message())
            )
            self.assertEqual(returned.path, source)
            self.assertEqual(returned.status, "executed")
            self.assertTrue(source.exists())
            self.assertFalse(shared.is_held(source))
            self.assertTrue(shared.try_acquire(source))
            shared.release(source)

    def test_lock_released_when_move_raises_on_manual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            service = InboxReclassifyService(path_lock=shared)

            classifier = MagicMock()
            classifier.classify_local_file = AsyncMock(
                side_effect=OSError("disk full")
            )

            with self.assertRaises(OSError):
                _run(
                    reclassify_inbox_file(
                        config,
                        "Inception 2010.mkv",
                        classifier=classifier,
                        service=service,
                    )
                )
            self.assertTrue(source.exists())
            self.assertFalse(shared.is_held(source))
            self.assertTrue(shared.try_acquire(source))
            shared.release(source)

    def test_two_distinct_files_classify_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            first = download_dir / "Inbox" / "Inception 2010.mkv"
            second = download_dir / "Inbox" / "Matrix 1999.mkv"
            first.write_bytes(b"video-a")
            second.write_bytes(b"video-b")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            service = InboxReclassifyService(path_lock=shared)

            both_inside = asyncio.Event()
            release = asyncio.Event()
            inside = 0

            class ParallelClassifier:
                async def classify_local_file(
                    self,
                    source_path: Path,
                    **_: Any,
                ) -> ClassificationResult:
                    nonlocal inside
                    inside += 1
                    if inside == 2:
                        both_inside.set()
                    await release.wait()
                    inside -= 1
                    return _classified_result(source_path)

            async def run() -> tuple[Any, Any]:
                classifier = ParallelClassifier()
                task_a = asyncio.create_task(
                    reclassify_inbox_file(
                        config,
                        "Inception 2010.mkv",
                        classifier=classifier,  # type: ignore[arg-type]
                        service=service,
                    )
                )
                task_b = asyncio.create_task(
                    reclassify_inbox_file(
                        config,
                        "Matrix 1999.mkv",
                        classifier=classifier,  # type: ignore[arg-type]
                        service=service,
                    )
                )
                await asyncio.wait_for(both_inside.wait(), timeout=2.0)
                release.set()
                return await task_a, await task_b

            result_a, result_b = _run(run())
            self.assertEqual(result_a.status, "classified")
            self.assertEqual(result_b.status, "classified")
            self.assertFalse(shared.is_held(first))
            self.assertFalse(shared.is_held(second))

    def test_same_file_never_processed_simultaneously(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            service = InboxReclassifyService(path_lock=shared)
            manager = DownloadManager(
                config,
                set(),
                classification_lock=shared,
            )

            concurrent = 0
            max_concurrent = 0
            gate = asyncio.Event()
            entered = asyncio.Event()

            async def track_classify(dest: Path, **_: Any) -> ClassificationResult:
                nonlocal concurrent, max_concurrent
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
                entered.set()
                await gate.wait()
                concurrent -= 1
                return _classified_result(dest)

            classifier = MagicMock()
            classifier.classify_and_relocate = AsyncMock(side_effect=track_classify)
            classifier.classify_local_file = AsyncMock(side_effect=track_classify)
            manager._classifier = classifier

            async def run() -> tuple[AutoClassifyOutcome, Any]:
                auto_task = asyncio.create_task(
                    manager._classify_downloaded_file(source, _message())
                )
                await entered.wait()
                manual = await reclassify_inbox_file(
                    config,
                    "Inception 2010.mkv",
                    classifier=classifier,
                    service=service,
                )
                gate.set()
                auto_outcome = await auto_task
                return auto_outcome, manual

            auto_outcome, manual = _run(run())
            self.assertEqual(max_concurrent, 1)
            self.assertEqual(manual.status, "already_processing")
            self.assertEqual(auto_outcome.path, source)
            self.assertEqual(auto_outcome.status, "executed")
            self.assertFalse(shared.is_held(source))

    def test_file_not_lost_under_contention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            payload = b"precious-video-bytes"
            source.write_bytes(payload)
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            service = InboxReclassifyService(path_lock=shared)
            manager = DownloadManager(
                config,
                set(),
                classification_lock=shared,
            )

            started = asyncio.Event()
            release = asyncio.Event()

            class SlowClassifier:
                async def classify_local_file(
                    self,
                    source_path: Path,
                    **_: Any,
                ) -> ClassificationResult:
                    started.set()
                    await release.wait()
                    # No mueve el archivo: simula fallo de movimiento.
                    return ClassificationResult(
                        "inbox",
                        "error_move:OSError",
                    )

            auto_classifier = MagicMock()
            auto_classifier.classify_and_relocate = AsyncMock(
                return_value=_classified_result(source)
            )
            manager._classifier = auto_classifier

            async def run() -> tuple[Any, AutoClassifyOutcome]:
                manual_task = asyncio.create_task(
                    reclassify_inbox_file(
                        config,
                        "Inception 2010.mkv",
                        classifier=SlowClassifier(),  # type: ignore[arg-type]
                        service=service,
                    )
                )
                await started.wait()
                auto_outcome = await manager._classify_downloaded_file(
                    source,
                    _message(),
                )
                release.set()
                manual = await manual_task
                return manual, auto_outcome

            manual, auto_outcome = _run(run())
            self.assertEqual(manual.status, "move_error")
            self.assertEqual(auto_outcome.path, source)
            self.assertEqual(auto_outcome.status, "deferred")
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), payload)
            self.assertFalse(shared.is_held(source))
            auto_classifier.classify_and_relocate.assert_not_awaited()

    def test_lock_not_held_during_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media" / "Downloads")
            config = _config(Path(tmp), download_dir)
            shared = ClassificationPathLock()
            manager = DownloadManager(
                config,
                set(),
                classification_lock=shared,
            )
            message = _message()
            held_during_download: list[bool] = []

            final = (
                config.download_dir / "Inbox" / "2026-11" / "Inception 2010.mkv"
            )

            async def fake_download_media(
                _message: Any,
                *,
                file: str,
                progress_callback: Any = None,
            ) -> None:
                # Durante la descarga el bloqueo de clasificación no debe estar activo.
                held_during_download.append(shared.is_held(final))
                Path(file).write_bytes(b"video")

            client = MagicMock()
            client.download_media = AsyncMock(side_effect=fake_download_media)

            classifier = MagicMock()
            classifier.classify_and_relocate = AsyncMock(
                return_value=ClassificationResult("inbox", "sin_resultados")
            )
            manager._classifier = classifier

            with (
                patch(
                    "telegram_downloader.download_manager.notify_async",
                    new=AsyncMock(),
                ),
                patch("telegram_downloader.download_manager.save_downloaded_ids"),
            ):
                _run(manager._download_one(client, message))

            self.assertEqual(held_during_download, [False])
            inbox_file = (
                config.download_dir / "Inbox" / "2026-11" / "Inception 2010.mkv"
            )
            self.assertFalse(shared.is_held(inbox_file))
            classifier.classify_and_relocate.assert_awaited_once()

    def test_production_singletons_share_same_lock(self) -> None:
        from telegram_downloader.dashboard.inbox_service import get_reclassify_service

        service = get_reclassify_service()
        self.assertIs(service._path_lock, get_classification_lock())


if __name__ == "__main__":
    unittest.main()
