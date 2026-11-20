"""Clasificación automática TMDb y movimiento en la biblioteca media (un solo mount)."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

TMDB_MULTI_SEARCH_URL = "https://api.themoviedb.org/3/search/multi"

LIBRARY_SIBLINGS = (
    "Movies",
    "Series",
    "Documentaries",
    "Home Videos",
)

_VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts"})

_EPISODE_PATTERNS = (
    re.compile(r"\b[Ss](\d{1,2})[Ee](\d{1,3})\b"),
    re.compile(r"\b(\d{1,2})[xX](\d{1,3})\b"),
)

_YEAR_PATTERN = re.compile(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)")

_NOISE_TOKENS = frozenset(
    {
        "1080p",
        "720p",
        "480p",
        "2160p",
        "4k",
        "8k",
        "uhd",
        "hdr",
        "hdr10",
        "dv",
        "web",
        "webdl",
        "web-dl",
        "webrip",
        "bluray",
        "blu-ray",
        "bdrip",
        "brrip",
        "hdrip",
        "dvdrip",
        "hdtv",
        "remux",
        "proper",
        "repack",
        "extended",
        "unrated",
        "directors",
        "cut",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "avc",
        "aac",
        "ac3",
        "dts",
        "ddp5",
        "ddp",
        "truehd",
        "atmos",
        "multi",
        "dual",
        "latino",
        "castellano",
        "spanish",
        "english",
        "subs",
        "sub",
        "subforced",
        "nf",
        "amzn",
        "dsnp",
        "hulu",
        "internal",
    }
)

_BRACKET_CONTENT = re.compile(r"[\(\[\{].*?[\)\]\}]")
_SEPARATORS = re.compile(r"[._+\-]+")
_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')


@dataclass(frozen=True)
class EpisodeInfo:
    season: int
    episode: int


@dataclass(frozen=True)
class ParsedMediaName:
    raw: str
    title_query: str
    year: int | None
    episode: EpisodeInfo | None


@dataclass(frozen=True)
class TmdbMatch:
    media_type: str
    tmdb_id: int
    title: str
    year: int | None
    score: float


@dataclass(frozen=True)
class ClassificationResult:
    status: str
    reason: str
    match: TmdbMatch | None = None
    parsed: ParsedMediaName | None = None
    destination: Path | None = None


class TmdbClient(Protocol):
    async def search_multi(self, query: str) -> list[dict[str, Any]]:
        """Busca títulos en TMDb /search/multi."""


def media_root_from_download_dir(download_dir: Path) -> Path:
    """Return the media library root that contains Downloads as a sibling.

    DOWNLOAD_DIR is expected to be ``…/Downloads`` (e.g. ``/data/media/Downloads``).
    Prefer the logical parent when the path already ends in ``Downloads`` so
    container paths stay stable (avoid macOS ``/var`` → ``/private/var``).
    """
    if download_dir.name == "Downloads":
        return download_dir.parent
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
        if not source.exists():
            raise RuntimeError(
                f"Source missing after failed cross-device move: {source}"
            ) from None
        raise

    source.unlink()
    return destination


def unique_destination(path: Path) -> Path | None:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(1, 1000):
        candidate = parent / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    return None


def _safe_title(name: str) -> str:
    cleaned = _WHITESPACE.sub(" ", name.strip()) or "video"
    cleaned = _INVALID_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(" .")
    return cleaned or "video"


class PersistentJsonCache:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        self._load()

    def get(self, key: str) -> list[dict[str, Any]] | None:
        value = self._data.get(key)
        if not isinstance(value, list):
            return None
        return value

    def set(self, key: str, value: list[dict[str, Any]]) -> None:
        self._data[key] = value
        self._save()

    def _load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}
            return
        self._data = raw if isinstance(raw, dict) else {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self._path)


class AsyncTmdbHttpClient:
    def __init__(
        self,
        api_key: str,
        *,
        language: str = "es-ES",
        timeout_seconds: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError("TMDb API key vacía")
        self._api_key = api_key
        self._language = language
        self._timeout = timeout_seconds

    async def search_multi(self, query: str) -> list[dict[str, Any]]:
        cleaned = query.strip()
        if not cleaned:
            return []
        payload = await asyncio.to_thread(self._request_search, cleaned)
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        return [item for item in results if isinstance(item, dict)]

    def _request_search(self, query: str) -> dict[str, Any]:
        request = self._build_request(query)
        body = self._read_response_body(request)
        return self._parse_payload(body)

    def _build_request(self, query: str) -> urllib.request.Request:
        params = urllib.parse.urlencode(
            {
                "api_key": self._api_key,
                "query": query,
                "language": self._language,
                "include_adult": "false",
            }
        )
        return urllib.request.Request(
            f"{TMDB_MULTI_SEARCH_URL}?{params}",
            headers={
                "Accept": "application/json",
                "User-Agent": "telegram-video-downloader",
            },
            method="GET",
        )

    def _read_response_body(self, request: urllib.request.Request) -> str:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"TMDb HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("TMDb no disponible") from exc

    @staticmethod
    def _parse_payload(body: str) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Respuesta TMDb inválida") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Respuesta TMDb inválida")
        return payload


class CachingTmdbClient:
    def __init__(self, inner: TmdbClient, cache: PersistentJsonCache) -> None:
        self._inner = inner
        self._cache = cache

    async def search_multi(self, query: str) -> list[dict[str, Any]]:
        cache_key = query.casefold().strip()
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        results = await self._inner.search_multi(query)
        self._cache.set(cache_key, results)
        return results


def extract_episode_info(text: str) -> EpisodeInfo | None:
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        season = int(match.group(1))
        episode = int(match.group(2))
        if season <= 0 or episode <= 0:
            return None
        return EpisodeInfo(season=season, episode=episode)
    return None


def extract_year(text: str) -> int | None:
    match = _YEAR_PATTERN.search(text)
    if not match:
        return None
    year = int(match.group(1))
    if 1900 <= year <= 2100:
        return year
    return None


def strip_extension(name: str) -> str:
    path = Path(name)
    if path.suffix.lower() in _VIDEO_EXTENSIONS:
        return path.stem
    return name


def normalize_title(text: str) -> str:
    without_ext = strip_extension(text)
    without_brackets = _BRACKET_CONTENT.sub(" ", without_ext)
    spaced = _SEPARATORS.sub(" ", without_brackets)
    tokens = []
    for token in spaced.split():
        lowered = token.casefold()
        if lowered in _NOISE_TOKENS:
            continue
        if re.fullmatch(r"\d{3,4}p", lowered):
            continue
        tokens.append(token)
    cleaned = " ".join(tokens)
    cleaned = _EPISODE_PATTERNS[0].sub(" ", cleaned)
    cleaned = _EPISODE_PATTERNS[1].sub(" ", cleaned)
    cleaned = _YEAR_PATTERN.sub(" ", cleaned)
    cleaned = _NON_ALNUM.sub(" ", cleaned.casefold())
    return _WHITESPACE.sub(" ", cleaned).strip()


def parse_media_name(*sources: str | None) -> ParsedMediaName | None:
    raw_parts = [part.strip() for part in sources if part and part.strip()]
    if not raw_parts:
        return None

    raw = " | ".join(raw_parts)
    joined = " ".join(raw_parts)
    episode = extract_episode_info(joined)
    year = extract_year(joined)

    title_query = ""
    for part in raw_parts:
        candidate = normalize_title(part)
        if len(candidate) > len(title_query):
            title_query = candidate

    if len(title_query) < 2:
        return None

    return ParsedMediaName(
        raw=raw,
        title_query=title_query,
        year=year,
        episode=episode,
    )


def title_similarity(left: str, right: str) -> float:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _result_title(item: dict[str, Any]) -> str | None:
    for key in ("title", "name", "original_title", "original_name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _result_year(item: dict[str, Any]) -> int | None:
    for key in ("release_date", "first_air_date"):
        value = item.get(key)
        if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    return None


def _apply_year_score(
    score: float,
    parsed_year: int | None,
    result_year: int | None,
) -> float:
    if parsed_year is None or result_year is None:
        return score
    if parsed_year == result_year:
        return min(1.0, score + 0.12)
    return max(0.0, score - 0.25)


def _apply_type_score(
    score: float,
    media_type: str,
    episode: EpisodeInfo | None,
) -> float:
    adjusted = score
    if episode is not None and media_type == "movie":
        adjusted = max(0.0, adjusted - 0.2)
    if episode is None and media_type == "tv":
        adjusted = max(0.0, adjusted - 0.05)
    return adjusted


def score_tmdb_result(
    item: dict[str, Any],
    parsed: ParsedMediaName,
) -> TmdbMatch | None:
    media_type = item.get("media_type")
    if media_type not in {"movie", "tv"}:
        return None

    title = _result_title(item)
    tmdb_id = item.get("id")
    if title is None or not isinstance(tmdb_id, int):
        return None

    result_year = _result_year(item)
    score = title_similarity(parsed.title_query, title)
    score = _apply_year_score(score, parsed.year, result_year)
    score = _apply_type_score(score, media_type, parsed.episode)
    return TmdbMatch(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=title,
        year=result_year,
        score=score,
    )


def _score_all_results(
    results: list[dict[str, Any]],
    parsed: ParsedMediaName,
) -> list[TmdbMatch]:
    scored = [
        match
        for match in (score_tmdb_result(item, parsed) for item in results)
        if match is not None
    ]
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored


def pick_best_match(
    results: list[dict[str, Any]],
    parsed: ParsedMediaName,
    *,
    confidence_threshold: float,
    ambiguity_delta: float,
) -> tuple[TmdbMatch | None, str]:
    scored = _score_all_results(results, parsed)
    if not scored:
        return None, "sin_resultados"

    best = scored[0]
    if best.score < confidence_threshold:
        return None, "confianza_insuficiente"

    contenders = [item for item in scored if item.score >= confidence_threshold]
    if _is_ambiguous(contenders, parsed, ambiguity_delta):
        return None, "ambiguo"
    if best.media_type == "tv" and parsed.episode is None:
        return None, "serie_sin_episodio"
    return best, "ok"


def _is_ambiguous(
    contenders: list[TmdbMatch],
    parsed: ParsedMediaName,
    ambiguity_delta: float,
) -> bool:
    if len(contenders) < 2:
        return False

    best = contenders[0]
    second = contenders[1]
    close_scores = best.score - second.score <= ambiguity_delta
    different_titles = normalize_title(best.title) != normalize_title(second.title)
    if close_scores and different_titles:
        return True

    if parsed.year is not None:
        return False

    best_key = normalize_title(best.title)
    years = {
        item.year
        for item in contenders
        if normalize_title(item.title) == best_key and item.year is not None
    }
    return len(years) > 1


def build_classified_destination(
    download_dir: Path,
    match: TmdbMatch,
    parsed: ParsedMediaName,
    filename: str,
) -> Path:
    """Películas/series van a hermanos de Downloads, nunca a Downloads/Media."""
    safe_title = _safe_title(match.title)
    if match.media_type == "movie":
        folder_name = safe_title
        if match.year is not None:
            folder_name = f"{safe_title} ({match.year})"
        return movies_dir(download_dir) / folder_name / filename

    assert parsed.episode is not None
    season_dir = f"Season {parsed.episode.season:02d}"
    return series_dir(download_dir) / safe_title / season_dir / filename


class MediaClassifier:
    def __init__(
        self,
        client: TmdbClient,
        *,
        download_dir: Path,
        confidence_threshold: float = 0.72,
        ambiguity_delta: float = 0.08,
    ) -> None:
        self._client = client
        self._download_dir = download_dir
        self._confidence_threshold = confidence_threshold
        self._ambiguity_delta = ambiguity_delta

    async def classify_and_relocate(
        self,
        source_path: Path,
        *,
        message_text: str | None,
        original_filename: str | None,
    ) -> ClassificationResult:
        if not source_path.exists():
            return ClassificationResult("skipped", "archivo_inexistente")

        parsed = parse_media_name(message_text, original_filename, source_path.name)
        if parsed is None:
            return ClassificationResult("inbox", "sin_informacion")

        match_result = await self._resolve_match(parsed)
        if match_result.status != "classified" or match_result.match is None:
            return match_result

        return self._relocate_match(source_path, parsed, match_result.match)

    async def _resolve_match(self, parsed: ParsedMediaName) -> ClassificationResult:
        try:
            results = await self._client.search_multi(parsed.title_query)
        except Exception as exc:
            return ClassificationResult(
                "inbox",
                f"error_tmdb:{exc.__class__.__name__}",
                parsed=parsed,
            )

        match, reason = pick_best_match(
            results,
            parsed,
            confidence_threshold=self._confidence_threshold,
            ambiguity_delta=self._ambiguity_delta,
        )
        if match is None:
            return ClassificationResult("inbox", reason, parsed=parsed)
        return ClassificationResult("classified", "ok", match=match, parsed=parsed)

    def _relocate_match(
        self,
        source_path: Path,
        parsed: ParsedMediaName,
        match: TmdbMatch,
    ) -> ClassificationResult:
        destination = build_classified_destination(
            self._download_dir,
            match,
            parsed,
            source_path.name,
        )
        final = unique_destination(destination)
        if final is None:
            return ClassificationResult(
                "inbox", "destino_ocupado", match=match, parsed=parsed
            )

        try:
            moved = move_without_overwrite(source_path, final)
        except Exception as exc:
            return ClassificationResult(
                "inbox",
                f"error_move:{exc.__class__.__name__}",
                match=match,
                parsed=parsed,
            )

        return ClassificationResult(
            "classified",
            "ok",
            match=match,
            parsed=parsed,
            destination=moved,
        )


def build_media_classifier(
    *,
    api_key: str,
    download_dir: Path,
    cache_file: Path,
    language: str,
    confidence_threshold: float,
    ambiguity_delta: float,
) -> MediaClassifier:
    http_client = AsyncTmdbHttpClient(api_key, language=language)
    cache = PersistentJsonCache(cache_file)
    client = CachingTmdbClient(http_client, cache)
    return MediaClassifier(
        client,
        download_dir=download_dir,
        confidence_threshold=confidence_threshold,
        ambiguity_delta=ambiguity_delta,
    )
