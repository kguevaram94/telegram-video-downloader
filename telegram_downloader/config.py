"""Carga y validación de configuración desde config.env."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in _TRUTHY


def _parse_float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"Valor numérico inválido: {value!r}") from exc


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    chat_id: int | None
    download_dir: Path
    session_name: str
    state_file: Path
    progress_file: Path
    manual_queue_file: Path
    notify_enabled: bool
    notify_app_name: str
    dashboard_host: str
    dashboard_port: int
    log_path: Path
    static_dir: Path
    tmdb_api_key: str | None = field(default=None, repr=False)
    tmdb_enabled: bool = False
    tmdb_confidence_threshold: float = 0.72
    tmdb_ambiguity_delta: float = 0.08
    tmdb_language: str = "es-ES"
    tmdb_cache_file: Path = field(default_factory=lambda: ROOT / "tmdb_cache.json")

    @property
    def classification_enabled(self) -> bool:
        return self.tmdb_enabled and bool(self.tmdb_api_key)


def _require_credentials(*, require_chat_id: bool) -> tuple[str, str, str | None]:
    api_id_raw = os.getenv("API_ID")
    api_hash = os.getenv("API_HASH")
    chat_id_raw = os.getenv("CHAT_ID")

    missing = [
        name
        for name, value in [("API_ID", api_id_raw), ("API_HASH", api_hash)]
        if not value
    ]
    if require_chat_id and not chat_id_raw:
        missing.append("CHAT_ID")

    if missing:
        print(f"Faltan variables en config.env: {', '.join(missing)}")
        print("Copia config.env.example a config.env y completa los valores.")
        sys.exit(1)

    assert api_id_raw is not None and api_hash is not None
    return api_id_raw, api_hash, chat_id_raw


def _load_tmdb_settings() -> tuple[str | None, bool, float, float, str, Path]:
    tmdb_api_key = (os.getenv("TMDB_API_KEY") or "").strip() or None
    confidence = _parse_float(os.getenv("TMDB_CONFIDENCE_THRESHOLD"), 0.72)
    ambiguity = _parse_float(os.getenv("TMDB_AMBIGUITY_DELTA"), 0.08)
    if not 0.0 <= confidence <= 1.0:
        raise RuntimeError("TMDB_CONFIDENCE_THRESHOLD debe estar entre 0 y 1")
    if not 0.0 <= ambiguity <= 1.0:
        raise RuntimeError("TMDB_AMBIGUITY_DELTA debe estar entre 0 y 1")

    enabled = _parse_bool(os.getenv("TMDB_ENABLED"), default=tmdb_api_key is not None)
    language = os.getenv("TMDB_LANGUAGE", "es-ES")
    data_dir = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
    default_cache = data_dir / "tmdb_cache.json"
    cache_file = Path(os.getenv("TMDB_CACHE_FILE", str(default_cache)))
    return tmdb_api_key, enabled, confidence, ambiguity, language, cache_file


def load_config(*, require_chat_id: bool = False) -> AppConfig:
    load_dotenv(ROOT / "config.env")
    api_id_raw, api_hash, chat_id_raw = _require_credentials(
        require_chat_id=require_chat_id,
    )
    chat_id = int(chat_id_raw) if chat_id_raw else None
    tmdb_api_key, tmdb_enabled, confidence, ambiguity, language, cache_file = (
        _load_tmdb_settings()
    )

    return AppConfig(
        api_id=int(api_id_raw),
        api_hash=api_hash,
        chat_id=chat_id,
        download_dir=Path(
            os.getenv("DOWNLOAD_DIR", str(Path.home() / "Videos" / "Telegram"))
        ),
        session_name=os.getenv("SESSION_NAME", str(ROOT / "telegram_session")),
        state_file=Path(os.getenv("STATE_FILE", str(ROOT / "downloaded.json"))),
        progress_file=Path(os.getenv("PROGRESS_FILE", str(ROOT / "downloader.progress"))),
        manual_queue_file=Path(
            os.getenv("MANUAL_QUEUE_FILE", str(ROOT / "manual_download_queue.json"))
        ),
        notify_enabled=_parse_bool(os.getenv("NOTIFY"), default=True),
        notify_app_name=os.getenv("NOTIFY_APP_NAME", "Telegram Downloader"),
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8765")),
        log_path=Path(os.getenv("LOG_PATH", str(ROOT / "downloader.log"))),
        static_dir=ROOT / "static",
        tmdb_api_key=tmdb_api_key,
        tmdb_enabled=tmdb_enabled,
        tmdb_confidence_threshold=confidence,
        tmdb_ambiguity_delta=ambiguity,
        tmdb_language=language,
        tmdb_cache_file=cache_file,
    )
