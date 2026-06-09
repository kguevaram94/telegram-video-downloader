"""Lectura de logs y progreso del descargador."""

from __future__ import annotations

import re
from pathlib import Path

_PROGRESS_LINE_RE = re.compile(r"(?:\[\+\d+ en cola\] )?.+: *\d+% ")


def read_log_tail(log_path: Path, max_lines: int = 200) -> list[str]:
    if not log_path.exists():
        return ["(sin log todavía)"]

    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return ["(error al leer el log)"]

    if not lines:
        return ["(log vacío)"]

    filtered = [line for line in lines if not _PROGRESS_LINE_RE.search(line)]
    if not filtered:
        return ["(log vacío)"]
    return filtered[-max_lines:]


def read_progress_line(progress_path: Path) -> str | None:
    if not progress_path.exists():
        return None

    try:
        line = progress_path.read_text(errors="replace").strip()
    except OSError:
        return None

    return line or None
