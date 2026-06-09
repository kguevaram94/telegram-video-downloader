"""Persistencia del estado de mensajes ya descargados."""

import json
from pathlib import Path


def load_downloaded_ids(state_file: Path) -> set[int]:
    if not state_file.exists():
        return set()

    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"No se pudo leer {state_file}") from exc

    if not isinstance(raw, list):
        raise RuntimeError(f"Formato inválido en {state_file}: se esperaba una lista")

    return {int(item) for item in raw}


def save_downloaded_ids(state_file: Path, downloaded_ids: set[int]) -> None:
    try:
        state_file.write_text(
            json.dumps(sorted(downloaded_ids)),
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeError(f"No se pudo guardar {state_file}") from exc
