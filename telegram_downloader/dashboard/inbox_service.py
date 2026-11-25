"""Listado y reclasificación manual de vídeos en Inbox."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram_downloader.config import AppConfig
from telegram_downloader.filenames import VIDEO_EXTENSIONS
from telegram_downloader.formatting import format_size
from telegram_downloader.media_classifier import (
    ClassificationResult,
    MediaClassifier,
    build_media_classifier,
    inbox_dir,
)

_NOT_FOUND_REASONS = frozenset(
    {
        "sin_informacion",
        "sin_resultados",
        "sin_resultados_tv",
        "confianza_insuficiente",
        "tipo_incorrecto",
        "serie_sin_episodio",
        "anio_no_coincide",
        "no_es_serie",
        "serie_incompleta",
        "sin_grupo_absoluto",
        "grupo_incompleto",
        "episodio_absoluto_no_encontrado",
    }
)


@dataclass(frozen=True)
class InboxVideoItem:
    relative_path: str
    filename: str
    size_bytes: int
    size_label: str
    modified_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "size_label": self.size_label,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True)
class ReclassifyResponse:
    ok: bool
    status: str
    filename: str
    reason: str
    relative_path: str | None = None
    media_type: str | None = None
    title: str | None = None
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    destination: str | None = None
    http_status: int = 200

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "filename": self.filename,
            "reason": self.reason,
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "title": self.title,
            "year": self.year,
            "season": self.season,
            "episode": self.episode,
            "destination": self.destination,
        }


class InboxReclassifyService:
    """Servicio con bloqueo por archivo para evitar reclasificaciones concurrentes."""

    def __init__(self) -> None:
        # threading.Lock evita depender de un event loop en el __init__ (Py3.9).
        self._lock = threading.Lock()
        self._processing: set[str] = set()

    def try_acquire(self, resolved_path: Path) -> bool:
        key = str(resolved_path)
        with self._lock:
            if key in self._processing:
                return False
            self._processing.add(key)
            return True

    def release(self, resolved_path: Path) -> None:
        key = str(resolved_path)
        with self._lock:
            self._processing.discard(key)


_reclassify_service = InboxReclassifyService()


def get_reclassify_service() -> InboxReclassifyService:
    return _reclassify_service


def list_inbox_videos(download_dir: Path) -> list[InboxVideoItem]:
    root = inbox_dir(download_dir)
    if not root.exists():
        return []

    items: list[InboxVideoItem] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        relative = path.relative_to(root).as_posix()
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        items.append(
            InboxVideoItem(
                relative_path=relative,
                filename=path.name,
                size_bytes=stat.st_size,
                size_label=format_size(stat.st_size),
                modified_at=modified.isoformat(),
            )
        )
    return items


def resolve_inbox_video_path(
    download_dir: Path,
    relative_path: str,
) -> tuple[Path | None, str | None]:
    """Resuelve una ruta relativa al Inbox con protección anti-traversal.

    Devuelve ``(path, error_status)``. ``error_status`` es None si es válido.
    """
    cleaned = (relative_path or "").strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
        return None, "outside_inbox"

    inbox_root = inbox_dir(download_dir).resolve()
    candidate = (inbox_root / cleaned).resolve()
    try:
        candidate.relative_to(inbox_root)
    except ValueError:
        return None, "outside_inbox"

    return candidate, None


def map_classification_status(result: ClassificationResult) -> str:
    if result.status == "classified":
        return "classified"
    if result.status == "skipped" or result.reason == "archivo_inexistente":
        return "invalid_file"

    reason = result.reason
    if reason == "ambiguo":
        return "ambiguous"
    if reason == "destino_ocupado":
        return "destination_occupied"
    if reason.startswith("error_tmdb"):
        return "tmdb_error"
    if reason.startswith("error_move"):
        return "move_error"
    if reason in _NOT_FOUND_REASONS:
        return "not_found"
    return "not_found"


def _build_classifier(config: AppConfig) -> MediaClassifier | None:
    if not config.classification_enabled or config.tmdb_api_key is None:
        return None
    return build_media_classifier(
        api_key=config.tmdb_api_key,
        download_dir=config.download_dir,
        cache_file=config.tmdb_cache_file,
        language=config.tmdb_language,
        confidence_threshold=config.tmdb_confidence_threshold,
        ambiguity_delta=config.tmdb_ambiguity_delta,
    )


def _response_from_result(
    *,
    result: ClassificationResult,
    source_path: Path,
    relative_path: str,
) -> ReclassifyResponse:
    status = map_classification_status(result)
    match = result.match
    parsed = result.parsed
    episode = parsed.episode if parsed is not None else None
    return ReclassifyResponse(
        ok=True,
        status=status,
        filename=source_path.name,
        reason=result.reason,
        relative_path=relative_path if status != "classified" else None,
        media_type=match.media_type if match else None,
        title=match.title if match else (parsed.title_query if parsed else None),
        year=match.year if match else (parsed.year if parsed else None),
        season=episode.season if episode else None,
        episode=episode.episode if episode else None,
        destination=str(result.destination) if result.destination else None,
    )


def _invalid_response(
    *,
    status: str,
    reason: str,
    filename: str,
    relative_path: str | None,
    http_status: int,
) -> ReclassifyResponse:
    return ReclassifyResponse(
        ok=False,
        status=status,
        filename=filename,
        reason=reason,
        relative_path=relative_path,
        http_status=http_status,
    )


async def reclassify_inbox_file(
    config: AppConfig,
    relative_path: str,
    *,
    classifier: MediaClassifier | None = None,
    service: InboxReclassifyService | None = None,
) -> ReclassifyResponse:
    """Reclasifica un vídeo local del Inbox sin volver a descargarlo."""
    lock_service = service or get_reclassify_service()
    resolved, path_error = resolve_inbox_video_path(config.download_dir, relative_path)
    if path_error is not None or resolved is None:
        return _invalid_response(
            status="outside_inbox",
            reason="ruta_fuera_de_inbox",
            filename=Path(relative_path).name if relative_path else "",
            relative_path=relative_path or None,
            http_status=400,
        )

    filename = resolved.name
    if not resolved.exists() or not resolved.is_file():
        return _invalid_response(
            status="invalid_file",
            reason="archivo_inexistente",
            filename=filename,
            relative_path=relative_path,
            http_status=404,
        )

    if resolved.suffix.lower() not in VIDEO_EXTENSIONS:
        return _invalid_response(
            status="invalid_file",
            reason="extension_no_permitida",
            filename=filename,
            relative_path=relative_path,
            http_status=400,
        )

    active_classifier = classifier
    if active_classifier is None:
        active_classifier = _build_classifier(config)
    if active_classifier is None:
        return _invalid_response(
            status="tmdb_error",
            reason="clasificacion_deshabilitada",
            filename=filename,
            relative_path=relative_path,
            http_status=503,
        )

    acquired = lock_service.try_acquire(resolved)
    if not acquired:
        return _invalid_response(
            status="already_processing",
            reason="ya_en_proceso",
            filename=filename,
            relative_path=relative_path,
            http_status=409,
        )

    try:
        # Solo el nombre actual del archivo: no caption ni nombre de Telegram.
        result = await active_classifier.classify_local_file(resolved)
        return _response_from_result(
            result=result,
            source_path=resolved,
            relative_path=relative_path,
        )
    finally:
        lock_service.release(resolved)
