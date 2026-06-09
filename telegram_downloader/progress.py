"""Reporte de progreso en terminal y archivo para el panel web."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from telegram_downloader.formatting import format_size


class ProgressFileStore:
    def __init__(self, progress_file: Path) -> None:
        self._progress_file = progress_file

    def clear(self) -> None:
        if self._progress_file.exists():
            self._progress_file.unlink()

    def write(self, text: str) -> None:
        self._progress_file.write_text(text, encoding="utf-8")


class ProgressReporter:
    def __init__(self, progress_store: ProgressFileStore) -> None:
        self._store = progress_store
        self._interactive = sys.stdout.isatty()
        self._last_line_len = 0
        self._last_pct = -1

    def status(self, text: str) -> None:
        if self._interactive:
            padded = text.ljust(max(len(text), self._last_line_len))
            sys.stdout.write(f"\r{padded}")
            sys.stdout.flush()
            self._last_line_len = len(padded)
            return

        self._store.write(text)

    def finish(self, text: str) -> None:
        if self._interactive:
            sys.stdout.write("\r" + " " * self._last_line_len + "\r")
            sys.stdout.flush()
            self._last_line_len = 0
            self._last_pct = -1
        else:
            self._store.clear()

        print(text, flush=True)

    def reset_pct(self) -> None:
        self._last_pct = -1

    def make_callback(
        self,
        label: str,
        queue_waiting: Callable[[], int],
    ) -> Callable[[int, int], None]:
        def on_progress(current: int, total: int) -> None:
            waiting = queue_waiting()
            cola = f"[+{waiting} en cola] " if waiting else ""

            if total:
                pct = int(current * 100 / total)
                if self._interactive and pct == self._last_pct:
                    return
                self._last_pct = pct
                self.status(
                    f"{cola}{label}: {pct:3d}% "
                    f"({format_size(current)} / {format_size(total)})"
                )
                return

            if not self._interactive:
                if current and current % (10 * 1024 * 1024) < 512 * 1024:
                    self._store.write(
                        f"{cola}{label}: {format_size(current)} descargados..."
                    )
                return

            self.status(f"{cola}{label}: {format_size(current)} descargados...")

        return on_progress
