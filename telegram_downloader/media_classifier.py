"""Clasificación automática TMDb y movimiento en la biblioteca media (un solo mount)."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
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

logger = logging.getLogger(__name__)

TMDB_API_BASE = "https://api.themoviedb.org/3"
TMDB_MULTI_SEARCH_URL = f"{TMDB_API_BASE}/search/multi"
TMDB_TV_URL = f"{TMDB_API_BASE}/tv"
TMDB_EPISODE_GROUP_URL = f"{TMDB_API_BASE}/tv/episode_group"

TMDB_EPISODE_GROUP_TYPE_ABSOLUTE = 2

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
_ABSOLUTE_EPISODE_TOKEN = re.compile(r"^\d{2,4}$")


@dataclass(frozen=True)
class EpisodeInfo:
    season: int
    episode: int


@dataclass(frozen=True)
class ParsedMediaName:
    """Metadatos normalizados del archivo.

    ``episode`` conserva la numeración convencional (SxxExx / NxN).
    ``absolute_episode`` es una pista independiente para anime; no sustituye
    ni borra el resultado convencional.
    """

    raw: str
    title_query: str
    year: int | None
    episode: EpisodeInfo | None
    absolute_episode: int | None = None


@dataclass(frozen=True)
class TmdbMatch:
    media_type: str
    tmdb_id: int
    title: str
    year: int | None
    score: float


@dataclass(frozen=True)
class AbsoluteEpisodeResolution:
    match: TmdbMatch
    episode: EpisodeInfo
    group_id: str
    group_name: str
    absolute_episode: int


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

    async def get_tv_details(self, series_id: int) -> dict[str, Any]:
        """Obtiene detalles de una serie en /tv/{series_id}."""

    async def get_tv_episode_groups(self, series_id: int) -> list[dict[str, Any]]:
        """Lista grupos de episodios en /tv/{series_id}/episode_groups."""

    async def get_tv_episode_group_details(self, group_id: str) -> dict[str, Any]:
        """Detalle de un grupo en /tv/episode_group/{id}."""


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

    def get(self, key: str) -> Any | None:
        if key not in self._data:
            return None
        return self._data.get(key)

    def get_list(self, key: str) -> list[dict[str, Any]] | None:
        value = self.get(key)
        if not isinstance(value, list):
            return None
        return [item for item in value if isinstance(item, dict)]

    def get_dict(self, key: str) -> dict[str, Any] | None:
        value = self.get(key)
        if not isinstance(value, dict):
            return None
        return value

    def set(self, key: str, value: Any) -> None:
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
        payload = await asyncio.to_thread(
            self._request_json,
            TMDB_MULTI_SEARCH_URL,
            {
                "query": cleaned,
                "include_adult": "false",
            },
        )
        return _dict_list(payload.get("results"))

    async def get_tv_details(self, series_id: int) -> dict[str, Any]:
        if series_id <= 0:
            raise ValueError("series_id inválido")
        return await asyncio.to_thread(
            self._request_json,
            f"{TMDB_TV_URL}/{series_id}",
            {},
        )

    async def get_tv_episode_groups(self, series_id: int) -> list[dict[str, Any]]:
        if series_id <= 0:
            raise ValueError("series_id inválido")
        payload = await asyncio.to_thread(
            self._request_json,
            f"{TMDB_TV_URL}/{series_id}/episode_groups",
            {},
        )
        return _dict_list(payload.get("results"))

    async def get_tv_episode_group_details(self, group_id: str) -> dict[str, Any]:
        cleaned = group_id.strip()
        if not cleaned:
            raise ValueError("group_id vacío")
        return await asyncio.to_thread(
            self._request_json,
            f"{TMDB_EPISODE_GROUP_URL}/{urllib.parse.quote(cleaned, safe='')}",
            {},
        )

    def _request_json(
        self,
        url: str,
        extra_params: dict[str, str],
    ) -> dict[str, Any]:
        params = {
            "api_key": self._api_key,
            "language": self._language,
            **extra_params,
        }
        request = urllib.request.Request(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers={
                "Accept": "application/json",
                "User-Agent": "telegram-video-downloader",
            },
            method="GET",
        )
        body = self._read_response_body(request)
        return self._parse_payload(body)

    def _read_response_body(self, request: urllib.request.Request) -> str:
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"TMDb HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("TMDb no disponible") from exc
        except TimeoutError as exc:
            raise RuntimeError("TMDb timeout") from exc

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
        cache_key = f"search_multi:{query.casefold().strip()}"
        cached = self._cache.get_list(cache_key)
        if cached is not None:
            return cached
        # Compatibilidad con caché previa (clave = query sin prefijo).
        legacy_key = query.casefold().strip()
        legacy = self._cache.get_list(legacy_key)
        if legacy is not None:
            return legacy
        results = await self._inner.search_multi(query)
        self._cache.set(cache_key, results)
        return results

    async def get_tv_details(self, series_id: int) -> dict[str, Any]:
        cache_key = f"tv_details:{series_id}"
        cached = self._cache.get_dict(cache_key)
        if cached is not None:
            return cached
        payload = await self._inner.get_tv_details(series_id)
        self._cache.set(cache_key, payload)
        return payload

    async def get_tv_episode_groups(self, series_id: int) -> list[dict[str, Any]]:
        cache_key = f"tv_episode_groups:{series_id}"
        cached = self._cache.get_list(cache_key)
        if cached is not None:
            return cached
        results = await self._inner.get_tv_episode_groups(series_id)
        self._cache.set(cache_key, results)
        return results

    async def get_tv_episode_group_details(self, group_id: str) -> dict[str, Any]:
        cache_key = f"tv_episode_group:{group_id.strip()}"
        cached = self._cache.get_dict(cache_key)
        if cached is not None:
            return cached
        payload = await self._inner.get_tv_episode_group_details(group_id)
        self._cache.set(cache_key, payload)
        return payload


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


def detect_absolute_episode_candidate(
    title_query: str,
    *,
    year: int | None,
) -> tuple[str, int] | None:
    """Detecta un candidato a episodio absoluto sin tocar SxxExx.

    Requiere un número de 2-4 dígitos tras el título. No trata años
    (1900-2100) como episodios. Los tokens posteriores al número se ignoran
    (p. ej. restos de ``WEB-DL`` → ``dl``).
    """
    tokens = title_query.split()
    if len(tokens) < 2:
        return None

    for index in range(len(tokens) - 1, 0, -1):
        token = tokens[index]
        if not _ABSOLUTE_EPISODE_TOKEN.fullmatch(token):
            continue
        number = int(token)
        if year is not None and number == year:
            continue
        if 1900 <= number <= 2100:
            continue
        title_tokens = tokens[:index]
        if not title_tokens:
            continue
        cleaned_title = " ".join(title_tokens).strip()
        if len(cleaned_title) < 2:
            continue
        return cleaned_title, number
    return None


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

    absolute_episode: int | None = None
    # SxxExx / NxN explícitos tienen prioridad sobre numeración absoluta.
    if episode is None:
        absolute = detect_absolute_episode_candidate(title_query, year=year)
        if absolute is not None:
            title_query, absolute_episode = absolute

    if len(title_query) < 2:
        return None

    return ParsedMediaName(
        raw=raw,
        title_query=title_query,
        year=year,
        episode=episode,
        absolute_episode=absolute_episode,
    )


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


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
    absolute_episode: int | None = None,
) -> float:
    adjusted = score
    has_episode_signal = episode is not None or absolute_episode is not None
    if has_episode_signal and media_type == "movie":
        adjusted = max(0.0, adjusted - 0.2)
    if not has_episode_signal and media_type == "tv":
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
    score = _apply_type_score(
        score,
        media_type,
        parsed.episode,
        parsed.absolute_episode,
    )
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


def pick_best_tv_match(
    results: list[dict[str, Any]],
    parsed: ParsedMediaName,
    *,
    confidence_threshold: float,
    ambiguity_delta: float,
) -> tuple[TmdbMatch | None, str]:
    """Selecciona una serie TV fiable; ignora películas y resultados no-TV."""
    tv_only = [item for item in results if item.get("media_type") == "tv"]
    scored = _score_all_results(tv_only, parsed)
    if not scored:
        return None, "sin_resultados_tv"

    best = scored[0]
    if best.score < confidence_threshold:
        return None, "confianza_insuficiente"

    contenders = [item for item in scored if item.score >= confidence_threshold]
    if _is_ambiguous(contenders, parsed, ambiguity_delta):
        return None, "ambiguo"
    return best, "ok"


def _absolute_group_preference(group: dict[str, Any]) -> tuple[int, int]:
    name = str(group.get("name") or "").casefold()
    score = 0
    if group.get("type") == TMDB_EPISODE_GROUP_TYPE_ABSOLUTE:
        score += 100
    if "absolute" in name:
        score += 50
    if "no special" in name:
        score += 25
    if "with special" in name:
        score -= 15
    episode_count = group.get("episode_count")
    count = episode_count if isinstance(episode_count, int) else 0
    return score, count


def select_absolute_episode_groups(
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ordena candidatos a grupo absoluto (type=2 o nombre Absolute)."""
    candidates = [
        group
        for group in groups
        if isinstance(group, dict)
        and isinstance(group.get("id"), str)
        and group.get("id").strip()
        and (
            group.get("type") == TMDB_EPISODE_GROUP_TYPE_ABSOLUTE
            or "absolute" in str(group.get("name") or "").casefold()
        )
    ]
    candidates.sort(key=_absolute_group_preference, reverse=True)
    return candidates


def flatten_episode_group_episodes(
    details: dict[str, Any],
) -> list[dict[str, Any]]:
    groups = details.get("groups")
    if not isinstance(groups, list):
        return []

    ordered_groups = sorted(
        [group for group in groups if isinstance(group, dict)],
        key=lambda group: group.get("order", 0)
        if isinstance(group.get("order"), int)
        else 0,
    )
    flattened: list[dict[str, Any]] = []
    for group in ordered_groups:
        episodes = group.get("episodes")
        if not isinstance(episodes, list):
            continue
        ordered_episodes = sorted(
            [episode for episode in episodes if isinstance(episode, dict)],
            key=lambda episode: episode.get("order", 0)
            if isinstance(episode.get("order"), int)
            else 0,
        )
        flattened.extend(ordered_episodes)
    return flattened


def _episode_link_from_payload(episode: dict[str, Any]) -> EpisodeInfo | None:
    season = episode.get("season_number")
    number = episode.get("episode_number")
    if not isinstance(season, int) or not isinstance(number, int):
        return None
    if season < 0 or number <= 0:
        return None
    return EpisodeInfo(season=season, episode=number)


def find_absolute_episode_in_group(
    details: dict[str, Any],
    absolute_episode: int,
) -> EpisodeInfo | None:
    """Resuelve el episodio absoluto usando el orden del grupo TMDb.

    No estima SxxExx; usa el enlace season_number/episode_number del grupo.
    """
    if absolute_episode <= 0:
        return None

    episodes = flatten_episode_group_episodes(details)
    if not episodes:
        return None

    for episode in episodes:
        order = episode.get("order")
        if isinstance(order, int) and order + 1 == absolute_episode:
            linked = _episode_link_from_payload(episode)
            if linked is not None:
                return linked

    if absolute_episode <= len(episodes):
        return _episode_link_from_payload(episodes[absolute_episode - 1])
    return None


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

        relocate_parsed = match_result.parsed or parsed
        return self._relocate_match(source_path, relocate_parsed, match_result.match)

    async def _resolve_match(self, parsed: ParsedMediaName) -> ClassificationResult:
        if parsed.absolute_episode is not None and parsed.episode is None:
            return await self._resolve_anime_absolute(parsed)
        return await self._resolve_conventional(parsed)

    async def _resolve_conventional(
        self,
        parsed: ParsedMediaName,
    ) -> ClassificationResult:
        logger.info(
            "[TMDB] Parse: title=%r year=%s episode=%s absolute=%s",
            parsed.title_query,
            parsed.year,
            _format_episode(parsed.episode),
            parsed.absolute_episode,
        )
        try:
            results = await self._client.search_multi(parsed.title_query)
        except Exception as exc:
            logger.info(
                "[TMDB] Rechazo: error_tmdb motivo=%s",
                exc.__class__.__name__,
            )
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
            logger.info("[TMDB] Rechazo: %s | destino=Inbox", reason)
            return ClassificationResult("inbox", reason, parsed=parsed)
        logger.info(
            "[TMDB] Clasificado: title=%r type=%s id=%s",
            match.title,
            match.media_type,
            match.tmdb_id,
        )
        return ClassificationResult("classified", "ok", match=match, parsed=parsed)

    async def _resolve_anime_absolute(
        self,
        parsed: ParsedMediaName,
    ) -> ClassificationResult:
        absolute = parsed.absolute_episode
        assert absolute is not None
        logger.info(
            "[TMDB] Anime detectado: title=%r absolute_episode=%s",
            parsed.title_query,
            absolute,
        )
        try:
            resolution = await self._resolve_absolute_episode(parsed, absolute)
        except Exception as exc:
            reason = f"error_tmdb:{exc.__class__.__name__}"
            logger.info("[TMDB] Anime sin clasificar: motivo=%s", reason)
            return ClassificationResult("inbox", reason, parsed=parsed)

        if isinstance(resolution, str):
            logger.info("[TMDB] Anime sin clasificar: motivo=%s", resolution)
            return ClassificationResult("inbox", resolution, parsed=parsed)

        resolved_parsed = ParsedMediaName(
            raw=parsed.raw,
            title_query=parsed.title_query,
            year=parsed.year,
            episode=resolution.episode,
            absolute_episode=parsed.absolute_episode,
        )
        logger.info(
            "[TMDB] Episodio absoluto validado: absolute=%s -> S%02dE%02d "
            "(group=%r)",
            resolution.absolute_episode,
            resolution.episode.season,
            resolution.episode.episode,
            resolution.group_name,
        )
        logger.info(
            "[TMDB] Clasificado: title=%r type=tv id=%s absolute=%s",
            resolution.match.title,
            resolution.match.tmdb_id,
            absolute,
        )
        return ClassificationResult(
            "classified",
            "ok",
            match=resolution.match,
            parsed=resolved_parsed,
        )

    async def _resolve_absolute_episode(
        self,
        parsed: ParsedMediaName,
        absolute: int,
    ) -> AbsoluteEpisodeResolution | str:
        try:
            results = await self._client.search_multi(parsed.title_query)
        except Exception as exc:
            return f"error_tmdb:{exc.__class__.__name__}"

        match, reason = pick_best_tv_match(
            results,
            parsed,
            confidence_threshold=self._confidence_threshold,
            ambiguity_delta=self._ambiguity_delta,
        )
        if match is None:
            return reason
        if match.media_type != "tv":
            return "no_es_serie"

        logger.info(
            "[TMDB] Serie identificada: title=%r id=%s score=%.3f",
            match.title,
            match.tmdb_id,
            match.score,
        )

        details_ok = await self._confirm_tv_series(match.tmdb_id)
        if not details_ok:
            return "serie_incompleta"

        try:
            groups = await self._client.get_tv_episode_groups(match.tmdb_id)
        except Exception as exc:
            return f"error_tmdb_groups:{exc.__class__.__name__}"

        logger.info(
            "[TMDB] Grupos de episodios encontrados: count=%s",
            len(groups),
        )
        absolute_groups = select_absolute_episode_groups(groups)
        if not absolute_groups:
            return "sin_grupo_absoluto"

        return await self._match_absolute_in_groups(
            match,
            absolute_groups,
            absolute,
        )

    async def _confirm_tv_series(self, series_id: int) -> bool:
        try:
            details = await self._client.get_tv_details(series_id)
        except Exception:
            return False
        name = details.get("name") or details.get("original_name")
        return isinstance(name, str) and bool(name.strip()) and "id" in details

    async def _match_absolute_in_groups(
        self,
        match: TmdbMatch,
        absolute_groups: list[dict[str, Any]],
        absolute: int,
    ) -> AbsoluteEpisodeResolution | str:
        last_reason = "episodio_absoluto_no_encontrado"
        for group in absolute_groups:
            group_id = str(group["id"]).strip()
            group_name = str(group.get("name") or group_id)
            logger.info(
                "[TMDB] Grupo seleccionado: id=%s name=%r type=%s",
                group_id,
                group_name,
                group.get("type"),
            )
            try:
                details = await self._client.get_tv_episode_group_details(group_id)
            except Exception as exc:
                last_reason = f"error_tmdb_group_detail:{exc.__class__.__name__}"
                continue

            if not isinstance(details.get("groups"), list):
                last_reason = "grupo_incompleto"
                continue

            episode = find_absolute_episode_in_group(details, absolute)
            if episode is None:
                last_reason = "episodio_absoluto_no_encontrado"
                continue

            return AbsoluteEpisodeResolution(
                match=match,
                episode=episode,
                group_id=group_id,
                group_name=group_name,
                absolute_episode=absolute,
            )
        return last_reason

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


def _format_episode(episode: EpisodeInfo | None) -> str | None:
    if episode is None:
        return None
    return f"S{episode.season:02d}E{episode.episode:02d}"


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
