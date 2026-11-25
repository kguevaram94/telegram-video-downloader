"""Tests de reclasificación manual de vídeos en Inbox."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from telegram_downloader.config import AppConfig
from telegram_downloader.dashboard.app import create_app
from telegram_downloader.dashboard.inbox_service import (
    InboxReclassifyService,
    list_inbox_videos,
    map_classification_status,
    reclassify_inbox_file,
    resolve_inbox_video_path,
)
from telegram_downloader.dashboard.service import ServicePaths
from telegram_downloader.media_classifier import (
    ClassificationResult,
    MediaClassifier,
    ParsedMediaName,
    TmdbMatch,
)


class FakeTmdbClient:
    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        *,
        tv_details: dict[str, Any] | None = None,
        episode_groups: list[dict[str, Any]] | None = None,
        episode_group_details: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.tv_details = tv_details or {}
        self.episode_groups = episode_groups or []
        self.episode_group_details = episode_group_details or {}
        self.calls: list[str] = []

    async def search_multi(self, query: str) -> list[dict[str, Any]]:
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return self.results

    async def get_tv_details(self, series_id: int) -> dict[str, Any]:
        if self.tv_details:
            return self.tv_details
        return {"id": series_id, "name": "Series"}

    async def get_tv_episode_groups(self, series_id: int) -> list[dict[str, Any]]:
        return self.episode_groups

    async def get_tv_episode_group_details(self, group_id: str) -> dict[str, Any]:
        details = self.episode_group_details.get(group_id)
        if details is None:
            raise RuntimeError("TMDb HTTP 404")
        return details


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _media_layout(root: Path) -> Path:
    download_dir = root / "Downloads"
    for path in (
        download_dir / "Inbox",
        root / "Movies",
        root / "Series",
        root / "Documentaries",
        root / "Home Videos",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return download_dir


def _config(tmp: Path, download_dir: Path) -> AppConfig:
    return AppConfig(
        api_id=1,
        api_hash="hash",
        chat_id=123,
        download_dir=download_dir,
        session_name=str(tmp / "session"),
        state_file=tmp / "downloaded.json",
        progress_file=tmp / "progress",
        manual_queue_file=tmp / "manual_queue.json",
        notify_enabled=False,
        notify_app_name="test",
        dashboard_host="127.0.0.1",
        dashboard_port=8765,
        log_path=tmp / "downloader.log",
        static_dir=tmp / "static",
        tmdb_api_key="test-key",
        tmdb_enabled=True,
        tmdb_cache_file=tmp / "tmdb_cache.json",
    )


def _service_paths(tmp: Path) -> ServicePaths:
    return ServicePaths(
        backend="process",
        plist_path=tmp / "unused.plist",
        service_label="test",
        downloader_script=tmp / "downloader.py",
        log_path=tmp / "downloader.log",
    )


def _absolute_group_details(
    *,
    absolute_episode: int,
    season: int,
    episode: int,
) -> dict[str, Any]:
    episodes = []
    for index in range(1, absolute_episode + 1):
        if index == absolute_episode:
            season_number, episode_number = season, episode
        else:
            season_number, episode_number = 1, index
        episodes.append(
            {
                "order": index - 1,
                "season_number": season_number,
                "episode_number": episode_number,
                "name": f"Episode {index}",
                "id": 1000 + index,
            }
        )
    return {
        "id": "abs-no-specials",
        "name": "Absolute (No Specials)",
        "type": 2,
        "groups": [{"order": 0, "episodes": episodes}],
    }


class ResolveInboxPathTests(unittest.TestCase):
    def test_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            path, error = resolve_inbox_video_path(
                download_dir,
                "../Movies/secret.mkv",
            )
            self.assertIsNone(path)
            self.assertEqual(error, "outside_inbox")

    def test_rejects_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            path, error = resolve_inbox_video_path(
                download_dir,
                str(download_dir / "Inbox" / "a.mkv"),
            )
            self.assertIsNone(path)
            self.assertEqual(error, "outside_inbox")

    def test_accepts_relative_inbox_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            target = download_dir / "Inbox" / "2024-01" / "Movie.mkv"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"video")
            path, error = resolve_inbox_video_path(download_dir, "2024-01/Movie.mkv")
            self.assertIsNone(error)
            assert path is not None
            self.assertEqual(path.resolve(), target.resolve())


class MapStatusTests(unittest.TestCase):
    def test_maps_known_reasons(self) -> None:
        self.assertEqual(
            map_classification_status(ClassificationResult("classified", "ok")),
            "classified",
        )
        self.assertEqual(
            map_classification_status(ClassificationResult("inbox", "ambiguo")),
            "ambiguous",
        )
        self.assertEqual(
            map_classification_status(ClassificationResult("inbox", "sin_resultados")),
            "not_found",
        )
        self.assertEqual(
            map_classification_status(
                ClassificationResult("inbox", "destino_ocupado")
            ),
            "destination_occupied",
        )
        self.assertEqual(
            map_classification_status(
                ClassificationResult("inbox", "error_tmdb:RuntimeError")
            ),
            "tmdb_error",
        )
        self.assertEqual(
            map_classification_status(
                ClassificationResult("inbox", "error_move:OSError")
            ),
            "move_error",
        )


class InboxReclassifyServiceTests(unittest.TestCase):
    def test_successful_movie_after_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            month = download_dir / "Inbox" / "2024-06"
            month.mkdir(parents=True)
            # Nombre corregido manualmente antes de reclasificar.
            source = month / "Inception 2010.mkv"
            source.write_bytes(b"movie-bytes")
            state_file = Path(tmp) / "downloaded.json"
            state_file.write_text(json.dumps([42]), encoding="utf-8")

            client = FakeTmdbClient(
                [
                    {
                        "id": 27205,
                        "media_type": "movie",
                        "title": "Inception",
                        "release_date": "2010-07-16",
                    }
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            config = _config(Path(tmp), download_dir)
            config.state_file.write_text(state_file.read_text(encoding="utf-8"))

            result = _run(
                reclassify_inbox_file(
                    config,
                    "2024-06/Inception 2010.mkv",
                    classifier=classifier,
                )
            )

            self.assertEqual(result.status, "classified")
            self.assertEqual(result.title, "Inception")
            self.assertEqual(result.year, 2010)
            self.assertEqual(result.media_type, "movie")
            assert result.destination is not None
            self.assertFalse(source.exists())
            self.assertTrue(Path(result.destination).exists())
            self.assertEqual(
                json.loads(config.state_file.read_text(encoding="utf-8")),
                [42],
            )
            self.assertEqual(client.calls[0], "Inception")

    def test_successful_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Chuck S01E01.mkv"
            source.write_bytes(b"series")
            client = FakeTmdbClient(
                [
                    {
                        "id": 1404,
                        "media_type": "tv",
                        "name": "Chuck",
                        "first_air_date": "2007-09-24",
                    }
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "Chuck S01E01.mkv",
                    classifier=classifier,
                )
            )
            self.assertEqual(result.status, "classified")
            self.assertEqual(result.media_type, "tv")
            self.assertEqual(result.season, 1)
            self.assertEqual(result.episode, 1)
            self.assertFalse(source.exists())

    def test_absolute_episode_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "One Piece 944.mkv"
            source.write_bytes(b"anime")
            client = FakeTmdbClient(
                results=[
                    {
                        "id": 37854,
                        "media_type": "tv",
                        "name": "One Piece",
                        "first_air_date": "1999-10-20",
                    }
                ],
                episode_groups=[
                    {
                        "id": "abs-no-specials",
                        "name": "Absolute (No Specials)",
                        "type": 2,
                    }
                ],
                episode_group_details={
                    "abs-no-specials": _absolute_group_details(
                        absolute_episode=944,
                        season=21,
                        episode=17,
                    )
                },
                tv_details={"id": 37854, "name": "One Piece"},
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "One Piece 944.mkv",
                    classifier=classifier,
                )
            )
            self.assertEqual(result.status, "classified")
            self.assertEqual(result.season, 21)
            self.assertEqual(result.episode, 17)
            self.assertFalse(source.exists())

    def test_ambiguous_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Dune.mkv"
            source.write_bytes(b"video")
            client = FakeTmdbClient(
                [
                    {
                        "id": 10,
                        "media_type": "movie",
                        "title": "Dune",
                        "release_date": "2021-10-01",
                    },
                    {
                        "id": 11,
                        "media_type": "movie",
                        "title": "Dune",
                        "release_date": "1984-12-01",
                    },
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "Dune.mkv",
                    classifier=classifier,
                )
            )
            self.assertEqual(result.status, "ambiguous")
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"video")

    def test_not_found_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Unknown.Show.S01E01.mkv"
            source.write_bytes(b"video")
            classifier = MediaClassifier(FakeTmdbClient([]), download_dir=download_dir)
            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "Unknown.Show.S01E01.mkv",
                    classifier=classifier,
                )
            )
            self.assertEqual(result.status, "not_found")
            self.assertTrue(source.exists())

    def test_tmdb_error_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            client = FakeTmdbClient(error=RuntimeError("TMDb HTTP 503"))
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "Inception 2010.mkv",
                    classifier=classifier,
                )
            )
            self.assertEqual(result.status, "tmdb_error")
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"video")

    def test_destination_occupied_keeps_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            client = FakeTmdbClient(
                [
                    {
                        "id": 27205,
                        "media_type": "movie",
                        "title": "Inception",
                        "release_date": "2010-07-16",
                    }
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            with patch(
                "telegram_downloader.media_classifier.unique_destination",
                return_value=None,
            ):
                result = _run(
                    reclassify_inbox_file(
                        _config(Path(tmp), download_dir),
                        "Inception 2010.mkv",
                        classifier=classifier,
                    )
                )
            self.assertEqual(result.status, "destination_occupied")
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"video")

    def test_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "missing.mkv",
                    classifier=MediaClassifier(
                        FakeTmdbClient([]),
                        download_dir=download_dir,
                    ),
                )
            )
            self.assertEqual(result.status, "invalid_file")
            self.assertEqual(result.http_status, 404)

    def test_non_video_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            source = download_dir / "Inbox" / "notes.txt"
            source.write_text("not a video", encoding="utf-8")
            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "notes.txt",
                    classifier=MediaClassifier(
                        FakeTmdbClient([]),
                        download_dir=download_dir,
                    ),
                )
            )
            self.assertEqual(result.status, "invalid_file")
            self.assertEqual(result.reason, "extension_no_permitida")
            self.assertTrue(source.exists())

    def test_outside_inbox_movies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            movie = root / "Movies" / "Inception (2010)" / "Inception.mkv"
            movie.parent.mkdir(parents=True)
            movie.write_bytes(b"already")
            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "../Movies/Inception (2010)/Inception.mkv",
                    classifier=MediaClassifier(
                        FakeTmdbClient([]),
                        download_dir=download_dir,
                    ),
                )
            )
            self.assertEqual(result.status, "outside_inbox")
            self.assertTrue(movie.exists())

    def test_concurrent_requests_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            service = InboxReclassifyService()

            async def run_both() -> tuple[Any, Any]:
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
                                raw=source_path.name,
                                title_query="Inception",
                                year=2010,
                                episode=None,
                                content_type="movie",
                                parser="test",
                                search_queries=("Inception",),
                                absolute_episode=None,
                            ),
                            destination=source_path,
                        )

                first = asyncio.create_task(
                    reclassify_inbox_file(
                        config,
                        "Inception 2010.mkv",
                        classifier=SlowClassifier(),  # type: ignore[arg-type]
                        service=service,
                    )
                )
                await started.wait()
                second = await reclassify_inbox_file(
                    config,
                    "Inception 2010.mkv",
                    classifier=SlowClassifier(),  # type: ignore[arg-type]
                    service=service,
                )
                release.set()
                first_result = await first
                return first_result, second

            first_result, second_result = _run(run_both())
            self.assertEqual(second_result.status, "already_processing")
            self.assertEqual(second_result.http_status, 409)
            self.assertEqual(first_result.status, "classified")

    def test_does_not_redownload_or_touch_download_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            config = _config(Path(tmp), download_dir)
            config.state_file.write_text("[99]", encoding="utf-8")

            classifier = MagicMock()
            classifier.classify_local_file = AsyncMock(
                return_value=ClassificationResult(
                    "inbox",
                    "sin_resultados",
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
                )
            )

            with patch(
                "telegram_downloader.dashboard.inbox_service.build_media_classifier",
                return_value=classifier,
            ):
                with patch(
                    "telegram_downloader.download_manager.DownloadManager"
                ) as download_manager_cls:
                    result = _run(reclassify_inbox_file(config, "Inception 2010.mkv"))
                    download_manager_cls.assert_not_called()

            classifier.classify_local_file.assert_awaited_once()
            self.assertEqual(result.status, "not_found")
            self.assertEqual(
                json.loads(config.state_file.read_text(encoding="utf-8")),
                [99],
            )
            self.assertTrue(source.exists())

    def test_move_error_keeps_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"keep-me")
            client = FakeTmdbClient(
                [
                    {
                        "id": 27205,
                        "media_type": "movie",
                        "title": "Inception",
                        "release_date": "2010-07-16",
                    }
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            with patch(
                "telegram_downloader.media_classifier.move_without_overwrite",
                side_effect=OSError("disk full"),
            ):
                result = _run(
                    reclassify_inbox_file(
                        _config(Path(tmp), download_dir),
                        "Inception 2010.mkv",
                        classifier=classifier,
                    )
                )
            self.assertEqual(result.status, "move_error")
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"keep-me")


class InboxListAndApiTests(unittest.TestCase):
    def test_list_inbox_videos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            video = download_dir / "Inbox" / "2025-01" / "Film.mkv"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"abc")
            (download_dir / "Inbox" / "readme.txt").write_text("x", encoding="utf-8")

            items = list_inbox_videos(download_dir)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].relative_path, "2025-01/Film.mkv")
            self.assertEqual(items[0].filename, "Film.mkv")

    def test_app_registers_inbox_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            download_dir = _media_layout(root / "media")
            static_dir = root / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            config = _config(root, download_dir)
            config = AppConfig(
                **{
                    **config.__dict__,
                    "static_dir": static_dir,
                }
            )
            app = create_app(config, _service_paths(root))
            paths = {route.path for route in app.routes if hasattr(route, "path")}
            self.assertIn("/api/inbox", paths)
            self.assertIn("/api/inbox/reclassify", paths)

    def test_reclassify_endpoint_payload_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = _media_layout(Path(tmp) / "media")
            source = download_dir / "Inbox" / "Inception 2010.mkv"
            source.write_bytes(b"video")
            destination = (
                Path(tmp) / "media" / "Movies" / "Inception (2010)" / source.name
            )
            fake_result = ClassificationResult(
                "classified",
                "ok",
                match=TmdbMatch(
                    media_type="movie",
                    tmdb_id=27205,
                    title="Inception",
                    year=2010,
                    score=0.95,
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
                destination=destination,
            )
            mock_classifier = MagicMock()
            mock_classifier.classify_local_file = AsyncMock(return_value=fake_result)

            result = _run(
                reclassify_inbox_file(
                    _config(Path(tmp), download_dir),
                    "Inception 2010.mkv",
                    classifier=mock_classifier,
                )
            )
            payload = result.to_dict()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "classified")
            self.assertEqual(payload["title"], "Inception")
            self.assertEqual(payload["year"], 2010)
            self.assertEqual(payload["destination"], str(destination))
            mock_classifier.classify_local_file.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
