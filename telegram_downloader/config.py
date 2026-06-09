"""Carga y validación de configuración desde config.env."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.lower() in _TRUTHY


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    chat_id: int | None
    download_dir: Path
    session_name: str
    state_file: Path
    progress_file: Path
    notify_enabled: bool
    notify_app_name: str
    dashboard_host: str
    dashboard_port: int
    log_path: Path
    static_dir: Path


def load_config(*, require_chat_id: bool = False) -> AppConfig:
    load_dotenv(ROOT / "config.env")

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
    chat_id = int(chat_id_raw) if chat_id_raw else None

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
        notify_enabled=_parse_bool(os.getenv("NOTIFY"), default=True),
        notify_app_name=os.getenv("NOTIFY_APP_NAME", "Telegram Downloader"),
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8765")),
        log_path=ROOT / "downloader.log",
        static_dir=ROOT / "static",
    )
