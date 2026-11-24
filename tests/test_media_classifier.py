"""Pruebas unitarias del clasificador TMDb completo."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from telegram_downloader.media_classifier import (
    AsyncTmdbHttpClient,
    CachingTmdbClient,
    ClassificationResult,
    EpisodeInfo,
    MediaClassifier,
    ParsedMediaName,
    PersistentJsonCache,
    detect_absolute_episode_candidate,
    extract_episode_info,
    find_absolute_episode_in_group,
    media_root_from_download_dir,
    move_without_overwrite,
    normalize_title,
    parse_media_name,
    parse_with_guessit,
    pick_best_match,
    select_absolute_episode_groups,
)


class FakeTmdbClient:
    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        results_by_query: dict[str, list[dict[str, Any]]] | None = None,
        *,
        tv_details: dict[str, Any] | None = None,
        episode_groups: list[dict[str, Any]] | None = None,
        episode_group_details: dict[str, dict[str, Any]] | None = None,
        groups_error: Exception | None = None,
        group_detail_error: Exception | None = None,
        tv_details_error: Exception | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.results_by_query = {
            key.casefold(): value for key, value in (results_by_query or {}).items()
        }
        self.tv_details = tv_details or {}
        self.episode_groups = episode_groups or []
        self.episode_group_details = episode_group_details or {}
        self.groups_error = groups_error
        self.group_detail_error = group_detail_error
        self.tv_details_error = tv_details_error
        self.calls: list[str] = []
        self.tv_detail_calls: list[int] = []
        self.group_calls: list[int] = []
        self.group_detail_calls: list[str] = []

    async def search_multi(self, query: str) -> list[dict[str, Any]]:
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        if self.results_by_query:
            return list(self.results_by_query.get(query.casefold(), []))
        return self.results

    async def get_tv_details(self, series_id: int) -> dict[str, Any]:
        self.tv_detail_calls.append(series_id)
        if self.tv_details_error is not None:
            raise self.tv_details_error
        if self.tv_details:
            return self.tv_details
        return {"id": series_id, "name": "Series"}

    async def get_tv_episode_groups(self, series_id: int) -> list[dict[str, Any]]:
        self.group_calls.append(series_id)
        if self.groups_error is not None:
            raise self.groups_error
        return self.episode_groups

    async def get_tv_episode_group_details(self, group_id: str) -> dict[str, Any]:
        self.group_detail_calls.append(group_id)
        if self.group_detail_error is not None:
            raise self.group_detail_error
        details = self.episode_group_details.get(group_id)
        if details is None:
            raise RuntimeError("TMDb HTTP 404")
        return details


def _one_piece_tv_result() -> dict[str, Any]:
    return {
        "id": 37854,
        "media_type": "tv",
        "name": "One Piece",
        "first_air_date": "1999-10-20",
    }


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


class NormalizeTitleTests(unittest.TestCase):
    def test_removes_quality_codec_and_extension(self) -> None:
        raw = "The.Matrix.1999.1080p.BluRay.x264.mkv"
        self.assertEqual(normalize_title(raw), "the matrix")

    def test_keeps_meaningful_words(self) -> None:
        self.assertIn(
            "breaking bad",
            normalize_title("Breaking.Bad.S01E02.720p.WEB-DL.mkv"),
        )

    def test_removes_standalone_video_format_tokens(self) -> None:
        self.assertEqual(
            normalize_title("Wayward Pines S02E09 480p mkv"),
            "wayward pines",
        )


class EpisodeParsingTests(unittest.TestCase):
    def test_s01e02_pattern(self) -> None:
        info = extract_episode_info("Show Name S01E02 1080p")
        assert info is not None
        self.assertEqual((info.season, info.episode), (1, 2))

    def test_1x02_pattern(self) -> None:
        info = extract_episode_info("Show Name 1x02 dual audio")
        assert info is not None
        self.assertEqual((info.season, info.episode), (1, 2))


class AbsoluteEpisodeDetectionTests(unittest.TestCase):
    def test_one_piece_944_plain(self) -> None:
        parsed = parse_media_name("One Piece 944.mp4")
        assert parsed is not None
        self.assertEqual(parsed.title_query.casefold(), "one piece")
        self.assertIsNone(parsed.episode)
        self.assertEqual(parsed.absolute_episode, 944)

    def test_one_piece_944_with_quality(self) -> None:
        parsed = parse_media_name("One Piece 944 1080p.mp4")
        assert parsed is not None
        self.assertEqual(parsed.absolute_episode, 944)
        self.assertEqual(parsed.title_query.casefold(), "one piece")

    def test_one_piece_944_release_name(self) -> None:
        parsed = parse_media_name("One.Piece.944.1080p.WEB-DL.mkv")
        assert parsed is not None
        self.assertEqual(parsed.absolute_episode, 944)
        self.assertEqual(parsed.title_query.casefold(), "one piece")

    def test_explicit_sxxexx_keeps_conventional(self) -> None:
        parsed = parse_media_name("One Piece S09E44.mkv")
        assert parsed is not None
        assert parsed.episode is not None
        self.assertEqual((parsed.episode.season, parsed.episode.episode), (9, 44))
        self.assertIsNone(parsed.absolute_episode)

    def test_naruto_absolute_candidate(self) -> None:
        parsed = parse_media_name("Naruto 125.mkv")
        assert parsed is not None
        self.assertEqual(parsed.absolute_episode, 125)
        self.assertEqual(parsed.title_query.casefold(), "naruto")

    def test_bleach_absolute_candidate(self) -> None:
        parsed = parse_media_name("Bleach 366.mp4")
        assert parsed is not None
        self.assertEqual(parsed.absolute_episode, 366)
        self.assertEqual(parsed.title_query.casefold(), "bleach")

    def test_chuck_conventional(self) -> None:
        parsed = parse_media_name("Chuck S01E01.mkv")
        assert parsed is not None
        assert parsed.episode is not None
        self.assertEqual((parsed.episode.season, parsed.episode.episode), (1, 1))
        self.assertIsNone(parsed.absolute_episode)

    def test_fringe_conventional(self) -> None:
        parsed = parse_media_name("Fringe S01E01.mkv")
        assert parsed is not None
        assert parsed.episode is not None
        self.assertEqual((parsed.episode.season, parsed.episode.episode), (1, 1))
        self.assertIsNone(parsed.absolute_episode)

    def test_movie_with_year_not_absolute(self) -> None:
        parsed = parse_media_name("The.Invite.2026.720P.mp4")
        assert parsed is not None
        self.assertIn("invite", parsed.title_query.casefold())
        self.assertEqual(parsed.year, 2026)
        self.assertIsNone(parsed.episode)
        self.assertIsNone(parsed.absolute_episode)

    def test_single_digit_not_absolute_candidate(self) -> None:
        self.assertIsNone(
            detect_absolute_episode_candidate("top gun 2", year=None)
        )


class AbsoluteGroupHelpersTests(unittest.TestCase):
    def test_prefers_absolute_no_specials(self) -> None:
        groups = [
            {
                "id": "with",
                "name": "Absolute (With Specials)",
                "type": 2,
                "episode_count": 50,
            },
            {
                "id": "none",
                "name": "Absolute (No Specials)",
                "type": 2,
                "episode_count": 40,
            },
            {
                "id": "dvd",
                "name": "DVD Order",
                "type": 3,
                "episode_count": 40,
            },
        ]
        selected = select_absolute_episode_groups(groups)
        self.assertEqual(selected[0]["id"], "none")

    def test_find_absolute_episode_uses_group_link(self) -> None:
        details = _absolute_group_details(
            absolute_episode=944,
            season=21,
            episode=7,
        )
        linked = find_absolute_episode_in_group(details, 944)
        assert linked is not None
        self.assertEqual((linked.season, linked.episode), (21, 7))

    def test_missing_absolute_episode_returns_none(self) -> None:
        details = _absolute_group_details(
            absolute_episode=10,
            season=1,
            episode=10,
        )
        self.assertIsNone(find_absolute_episode_in_group(details, 944))


class ParseMediaNameTests(unittest.TestCase):
    def test_insufficient_information(self) -> None:
        self.assertIsNone(parse_media_name("a.mp4"))
        self.assertIsNone(parse_media_name(None, "", "   "))

    def test_movie_with_year(self) -> None:
        parsed = parse_media_name("Inception (2010) 1080p.mkv")
        assert parsed is not None
        self.assertEqual(parsed.title_query.casefold(), "inception")
        self.assertEqual(parsed.year, 2010)
        self.assertIsNone(parsed.episode)

    def test_series_from_caption_and_filename(self) -> None:
        parsed = parse_media_name(
            "Breaking Bad S02E03",
            "breaking.bad.s02e03.720p.mkv",
        )
        assert parsed is not None
        assert parsed.episode is not None
        self.assertEqual(parsed.episode.season, 2)
        self.assertEqual(parsed.episode.episode, 3)

    def test_chuck_series_keeps_title_without_release_group(self) -> None:
        parsed = parse_media_name("Chuck S01E01 720p BluRay x265 HETeam.mkv")
        assert parsed is not None
        assert parsed.episode is not None
        self.assertEqual(parsed.title_query, "Chuck")
        self.assertEqual(parsed.episode.season, 1)
        self.assertEqual(parsed.episode.episode, 1)
        self.assertEqual(parsed.release_group, "HETeam")
        self.assertEqual(parsed.parser, "guessit")
        self.assertNotIn("heteam", parsed.title_query.casefold())

    def test_wayward_pines_series(self) -> None:
        parsed = parse_media_name("Wayward Pines S02E09 480p mkv.mkv")
        assert parsed is not None
        assert parsed.episode is not None
        self.assertEqual(parsed.title_query, "Wayward Pines")
        self.assertEqual(parsed.episode.season, 2)
        self.assertEqual(parsed.episode.episode, 9)

    def test_movie_invite_strips_distribution_handle(self) -> None:
        parsed = parse_media_name("The.Invite.2026.720P.@TylerMoviesEmpire03.mp4")
        assert parsed is not None
        self.assertEqual(parsed.title_query, "The Invite")
        self.assertEqual(parsed.year, 2026)
        self.assertIsNone(parsed.episode)
        self.assertEqual(parsed.content_type, "movie")

    def test_separators_dots_dashes_underscores(self) -> None:
        dotted = parse_media_name("Breaking.Bad.S01E02.720p.mkv")
        dashed = parse_media_name("Breaking-Bad-S01E02-720p.mkv")
        underscored = parse_media_name("Breaking_Bad_S01E02_720p.mkv")
        for parsed in (dotted, dashed, underscored):
            assert parsed is not None
            self.assertEqual(parsed.title_query.casefold(), "breaking bad")
            assert parsed.episode is not None
            self.assertEqual((parsed.episode.season, parsed.episode.episode), (1, 2))

    def test_title_with_legitimate_numbers(self) -> None:
        parsed = parse_media_name("The.100.S01E01.mkv")
        assert parsed is not None
        self.assertEqual(parsed.title_query, "The 100")
        assert parsed.episode is not None
        self.assertEqual(parsed.episode.season, 1)
        self.assertEqual(parsed.episode.episode, 1)

    def test_name_without_season_episode(self) -> None:
        parsed = parse_media_name("Inception.2010.1080p.BluRay.mkv")
        assert parsed is not None
        self.assertEqual(parsed.title_query, "Inception")
        self.assertEqual(parsed.year, 2010)
        self.assertIsNone(parsed.episode)

    def test_guessit_direct_extracts_release_metadata(self) -> None:
        parsed = parse_with_guessit(
            "Movie.Title.2020.1080p.BluRay.x264-GROUP.mkv"
        )
        assert parsed is not None
        self.assertEqual(parsed.title_query, "Movie Title")
        self.assertEqual(parsed.year, 2020)
        self.assertEqual(parsed.release_group, "GROUP")
        self.assertEqual(parsed.screen_size, "1080p")
        self.assertEqual(parsed.video_codec, "H.264")
        self.assertEqual(parsed.source, "Blu-ray")


class MatchSelectionTests(unittest.TestCase):
    def test_movie_match_prefers_title_and_year(self) -> None:
        parsed = ParsedMediaName(
            raw="Inception 2010",
            title_query="inception",
            year=2010,
            episode=None,
        )
        results = [
            {
                "id": 1,
                "media_type": "movie",
                "title": "Inception",
                "release_date": "2010-07-16",
                "popularity": 10,
            },
            {
                "id": 2,
                "media_type": "movie",
                "title": "Inception Memory",
                "release_date": "2020-01-01",
                "popularity": 9999,
            },
        ]
        match, reason = pick_best_match(
            results,
            parsed,
            confidence_threshold=0.72,
            ambiguity_delta=0.08,
        )
        assert match is not None
        self.assertEqual(reason, "ok")
        self.assertEqual(match.tmdb_id, 1)
        self.assertEqual(match.media_type, "movie")

    def test_ambiguous_results_stay_unclassified(self) -> None:
        parsed = ParsedMediaName(
            raw="Dune",
            title_query="dune",
            year=None,
            episode=None,
        )
        results = [
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
        match, reason = pick_best_match(
            results,
            parsed,
            confidence_threshold=0.72,
            ambiguity_delta=0.08,
        )
        self.assertIsNone(match)
        self.assertEqual(reason, "ambiguo")

    def test_series_without_episode_stays_inbox(self) -> None:
        parsed = ParsedMediaName(
            raw="Breaking Bad",
            title_query="breaking bad",
            year=None,
            episode=None,
        )
        results = [
            {
                "id": 20,
                "media_type": "tv",
                "name": "Breaking Bad",
                "first_air_date": "2008-01-20",
            }
        ]
        match, reason = pick_best_match(
            results,
            parsed,
            confidence_threshold=0.72,
            ambiguity_delta=0.08,
        )
        self.assertIsNone(match)
        self.assertEqual(reason, "serie_sin_episodio")


class ClassifierIntegrationTests(unittest.TestCase):
    def test_classifies_movie_into_sibling_movies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Inception.2010.1080p.mkv"
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
            classifier = MediaClassifier(
                client, download_dir=download_dir, confidence_threshold=0.72
            )
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text="Inception 2010",
                    original_filename="Inception.2010.1080p.mkv",
                )
            )

            self.assertEqual(result.status, "classified")
            assert result.destination is not None
            self.assertTrue(result.destination.exists())
            self.assertFalse(source.exists())
            self.assertEqual(
                result.destination,
                root / "Movies" / "Inception (2010)" / "Inception.2010.1080p.mkv",
            )
            self.assertNotIn("Downloads/Media", str(result.destination))
            self.assertEqual(
                media_root_from_download_dir(download_dir),
                root,
            )

    def test_classifies_series_into_sibling_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Breaking.Bad.S01E02.mkv"
            source.write_bytes(b"video")

            client = FakeTmdbClient(
                [
                    {
                        "id": 1396,
                        "media_type": "tv",
                        "name": "Breaking Bad",
                        "first_air_date": "2008-01-20",
                    }
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text="Breaking Bad S01E02",
                    original_filename="Breaking.Bad.S01E02.mkv",
                )
            )

            self.assertEqual(result.status, "classified")
            assert result.destination is not None
            self.assertEqual(
                result.destination,
                root
                / "Series"
                / "Breaking Bad"
                / "Season 01"
                / "Breaking.Bad.S01E02.mkv",
            )
            self.assertTrue(result.destination.exists())

    def test_api_error_keeps_file_in_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Unknown.Movie.mkv"
            source.write_bytes(b"video")

            client = FakeTmdbClient(error=RuntimeError("TMDb HTTP 500"))
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text="Unknown Movie",
                    original_filename="Unknown.Movie.mkv",
                )
            )

            self.assertEqual(result.status, "inbox")
            self.assertTrue(result.reason.startswith("error_tmdb:"))
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"video")

    def test_move_failure_keeps_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Inception.mkv"
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
                    classifier.classify_and_relocate(
                        source,
                        message_text="Inception 2010",
                        original_filename="Inception.mkv",
                    )
                )

            self.assertEqual(result.status, "inbox")
            self.assertTrue(result.reason.startswith("error_move:"))
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"keep-me")

    def test_does_not_overwrite_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Inception.mkv"
            source.write_bytes(b"new")

            existing = root / "Movies" / "Inception (2010)" / "Inception.mkv"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"old")

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
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text="Inception 2010",
                    original_filename="Inception.mkv",
                )
            )

            self.assertEqual(result.status, "classified")
            assert result.destination is not None
            self.assertNotEqual(result.destination, existing)
            self.assertEqual(existing.read_bytes(), b"old")
            self.assertTrue(result.destination.exists())
            self.assertEqual(result.destination.name, "Inception_1.mkv")

    def test_chuck_classified_as_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Chuck S01E01 720p BluRay x265 HETeam.mkv"
            source.write_bytes(b"video")

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
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename=source.name,
                )
            )

            self.assertEqual(result.status, "classified")
            assert result.match is not None
            self.assertEqual(result.match.media_type, "tv")
            assert result.parsed is not None
            assert result.parsed.episode is not None
            self.assertEqual(result.parsed.title_query, "Chuck")
            self.assertEqual(result.parsed.episode.season, 1)
            self.assertEqual(result.parsed.episode.episode, 1)
            assert result.destination is not None
            self.assertEqual(
                result.destination,
                root / "Series" / "Chuck" / "Season 01" / source.name,
            )
            self.assertEqual(client.calls[0], "Chuck")

    def test_wayward_pines_classified_as_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Wayward Pines S02E09 480p mkv.mkv"
            source.write_bytes(b"video")

            client = FakeTmdbClient(
                [
                    {
                        "id": 53425,
                        "media_type": "tv",
                        "name": "Wayward Pines",
                        "first_air_date": "2015-05-14",
                    }
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text="Wayward Pines S02E09",
                    original_filename=source.name,
                )
            )

            self.assertEqual(result.status, "classified")
            assert result.destination is not None
            self.assertEqual(
                result.destination,
                root
                / "Series"
                / "Wayward Pines"
                / "Season 02"
                / source.name,
            )

    def test_invite_movie_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            filename = "The.Invite.2026.720P.@TylerMoviesEmpire03.mp4"
            source = download_dir / "Inbox" / filename
            source.write_bytes(b"video")

            client = FakeTmdbClient(
                [
                    {
                        "id": 999001,
                        "media_type": "movie",
                        "title": "The Invite",
                        "release_date": "2026-01-15",
                    }
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename=filename,
                )
            )

            self.assertEqual(result.status, "classified")
            assert result.match is not None
            self.assertEqual(result.match.media_type, "movie")
            assert result.parsed is not None
            self.assertEqual(result.parsed.title_query, "The Invite")
            self.assertEqual(result.parsed.year, 2026)
            assert result.destination is not None
            self.assertEqual(
                result.destination,
                root / "Movies" / "The Invite (2026)" / filename,
            )

    def test_empty_tmdb_keeps_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Unknown.Show.S01E01.mkv"
            source.write_bytes(b"video")

            client = FakeTmdbClient([])
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename=source.name,
                )
            )

            self.assertEqual(result.status, "inbox")
            self.assertEqual(result.reason, "sin_resultados")
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"video")

    def test_wrong_media_type_keeps_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Chuck S01E01.mkv"
            source.write_bytes(b"video")

            client = FakeTmdbClient(
                [
                    {
                        "id": 1,
                        "media_type": "movie",
                        "title": "Chuck",
                        "release_date": "2011-01-01",
                    }
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename=source.name,
                )
            )

            self.assertEqual(result.status, "inbox")
            self.assertEqual(result.reason, "tipo_incorrecto")
            self.assertTrue(source.exists())

    def test_ambiguous_results_keep_inbox(self) -> None:
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
                classifier.classify_and_relocate(
                    source,
                    message_text="Dune",
                    original_filename="Dune.mkv",
                )
            )

            self.assertEqual(result.status, "inbox")
            self.assertEqual(result.reason, "ambiguo")
            self.assertTrue(source.exists())

    def test_progressive_search_avoids_duplicate_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Chuck S01E01 720p BluRay x265 HETeam.mkv"
            source.write_bytes(b"video")

            client = FakeTmdbClient(
                results_by_query={
                    "Chuck": [],
                    "chuck heteam": [
                        {
                            "id": 1404,
                            "media_type": "tv",
                            "name": "Chuck",
                            "first_air_date": "2007-09-24",
                        }
                    ],
                }
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename=source.name,
                )
            )

            self.assertEqual(result.status, "classified")
            self.assertEqual(client.calls, ["Chuck", "chuck heteam"])
            self.assertEqual(len(client.calls), len(set(c.casefold() for c in client.calls)))

    def test_one_piece_absolute_episode_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "One Piece 944.mp4"
            source.write_bytes(b"video")

            client = FakeTmdbClient(
                [_one_piece_tv_result()],
                tv_details={"id": 37854, "name": "One Piece"},
                episode_groups=[
                    {
                        "id": "abs-no-specials",
                        "name": "Absolute (No Specials)",
                        "type": 2,
                        "episode_count": 944,
                    }
                ],
                episode_group_details={
                    "abs-no-specials": _absolute_group_details(
                        absolute_episode=944,
                        season=21,
                        episode=7,
                    )
                },
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename="One Piece 944.mp4",
                )
            )

            self.assertEqual(result.status, "classified")
            assert result.destination is not None
            self.assertEqual(
                result.destination,
                root / "Series" / "One Piece" / "Season 21" / "One Piece 944.mp4",
            )
            assert result.parsed is not None
            self.assertEqual(result.parsed.absolute_episode, 944)
            assert result.parsed.episode is not None
            self.assertEqual(result.parsed.episode.season, 21)
            self.assertEqual(result.parsed.episode.episode, 7)

    def test_ambiguous_absolute_series_matches_stay_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "One Piece 944.mp4"
            source.write_bytes(b"video")
            client = FakeTmdbClient(
                [
                    {
                        "id": 1,
                        "media_type": "tv",
                        "name": "One Piece",
                        "first_air_date": "1999-10-20",
                    },
                    {
                        "id": 2,
                        "media_type": "tv",
                        "name": "One Piece",
                        "first_air_date": "2023-08-31",
                    },
                ]
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename="One Piece 944.mp4",
                )
            )
            self.assertEqual(result.status, "inbox")
            self.assertEqual(result.reason, "ambiguo")
            self.assertTrue(source.exists())
            self.assertEqual(client.group_calls, [])

    def test_missing_absolute_group_stays_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Naruto 125.mkv"
            source.write_bytes(b"video")
            client = FakeTmdbClient(
                [
                    {
                        "id": 46260,
                        "media_type": "tv",
                        "name": "Naruto",
                        "first_air_date": "2002-10-03",
                    }
                ],
                tv_details={"id": 46260, "name": "Naruto"},
                episode_groups=[
                    {
                        "id": "dvd",
                        "name": "DVD Order",
                        "type": 3,
                        "episode_count": 220,
                    }
                ],
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename="Naruto 125.mkv",
                )
            )
            self.assertEqual(result.status, "inbox")
            self.assertEqual(result.reason, "sin_grupo_absoluto")
            self.assertTrue(source.exists())

    def test_absolute_episode_not_found_stays_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "Bleach 366.mp4"
            source.write_bytes(b"video")
            client = FakeTmdbClient(
                [
                    {
                        "id": 30984,
                        "media_type": "tv",
                        "name": "Bleach",
                        "first_air_date": "2004-10-05",
                    }
                ],
                tv_details={"id": 30984, "name": "Bleach"},
                episode_groups=[
                    {
                        "id": "abs",
                        "name": "Absolute",
                        "type": 2,
                        "episode_count": 10,
                    }
                ],
                episode_group_details={
                    "abs": _absolute_group_details(
                        absolute_episode=10,
                        season=1,
                        episode=10,
                    )
                },
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename="Bleach 366.mp4",
                )
            )
            self.assertEqual(result.status, "inbox")
            self.assertEqual(result.reason, "episodio_absoluto_no_encontrado")
            self.assertTrue(source.exists())

    def test_incomplete_group_response_stays_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "One Piece 944.mp4"
            source.write_bytes(b"video")
            client = FakeTmdbClient(
                [_one_piece_tv_result()],
                tv_details={"id": 37854, "name": "One Piece"},
                episode_groups=[
                    {
                        "id": "broken",
                        "name": "Absolute",
                        "type": 2,
                        "episode_count": 1,
                    }
                ],
                episode_group_details={"broken": {"id": "broken", "name": "Absolute"}},
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename="One Piece 944.mp4",
                )
            )
            self.assertEqual(result.status, "inbox")
            self.assertEqual(result.reason, "grupo_incompleto")
            self.assertTrue(source.exists())

    def test_http_timeout_on_groups_keeps_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "One Piece 944.mp4"
            source.write_bytes(b"video")
            client = FakeTmdbClient(
                [_one_piece_tv_result()],
                tv_details={"id": 37854, "name": "One Piece"},
                groups_error=RuntimeError("TMDb timeout"),
            )
            classifier = MediaClassifier(client, download_dir=download_dir)
            result = _run(
                classifier.classify_and_relocate(
                    source,
                    message_text=None,
                    original_filename="One Piece 944.mp4",
                )
            )
            self.assertEqual(result.status, "inbox")
            self.assertTrue(result.reason.startswith("error_tmdb_groups:"))
            self.assertTrue(source.exists())


class MatchValidationTests(unittest.TestCase):
    def test_wrong_type_rejected_by_pick_best_match(self) -> None:
        parsed = ParsedMediaName(
            raw="Chuck S01E01",
            title_query="Chuck",
            year=None,
            episode=EpisodeInfo(season=1, episode=1),
            content_type="episode",
        )
        results = [
            {
                "id": 1,
                "media_type": "movie",
                "title": "Chuck",
                "release_date": "2011-01-01",
            }
        ]
        match, reason = pick_best_match(
            results,
            parsed,
            confidence_threshold=0.72,
            ambiguity_delta=0.08,
        )
        self.assertIsNone(match)
        self.assertEqual(reason, "tipo_incorrecto")


class CacheAndHttpTests(unittest.TestCase):
    def test_persistent_cache_avoids_repeated_client_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "cache.json"
            cache = PersistentJsonCache(cache_file)
            inner = FakeTmdbClient(
                [{"id": 1, "media_type": "movie", "title": "Matrix"}]
            )
            client = CachingTmdbClient(inner, cache)

            first = _run(client.search_multi("Matrix"))
            second = _run(client.search_multi("matrix"))

            self.assertEqual(first, second)
            self.assertEqual(inner.calls, ["Matrix"])
            saved = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertIn("search_multi:matrix", saved)

    def test_episode_group_details_are_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "cache.json"
            cache = PersistentJsonCache(cache_file)
            details = {"id": "g1", "groups": []}
            inner = FakeTmdbClient(episode_group_details={"g1": details})
            client = CachingTmdbClient(inner, cache)

            first = _run(client.get_tv_episode_group_details("g1"))
            second = _run(client.get_tv_episode_group_details("g1"))

            self.assertEqual(first, second)
            self.assertEqual(inner.group_detail_calls, ["g1"])

    def test_http_client_builds_search_request_without_logging_key(self) -> None:
        client = AsyncTmdbHttpClient("secret-key", language="es-ES")

        class FakeResponse:
            def read(self) -> bytes:
                return b'{"results":[]}'

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *args: object) -> None:
                return None

        with patch(
            "telegram_downloader.media_classifier.urllib.request.urlopen",
            return_value=FakeResponse(),
        ) as mocked:
            results = _run(client.search_multi("Inception"))

        self.assertEqual(results, [])
        request = mocked.call_args.args[0]
        self.assertIn("query=Inception", request.full_url)
        self.assertIn("api_key=secret-key", request.full_url)


class ClassificationResultContractTests(unittest.TestCase):
    def test_result_defaults(self) -> None:
        result = ClassificationResult("inbox", "sin_informacion")
        self.assertIsNone(result.match)
        self.assertIsNone(result.destination)


class MoveSafetyTests(unittest.TestCase):
    def test_move_without_overwrite_refuses_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            download_dir = _media_layout(root)
            source = download_dir / "Inbox" / "clip.mp4"
            source.write_bytes(b"new")
            dest = root / "Movies" / "clip.mp4"
            dest.write_bytes(b"old")

            with self.assertRaises(FileExistsError):
                move_without_overwrite(source, dest)

            self.assertTrue(source.exists())
            self.assertEqual(dest.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
