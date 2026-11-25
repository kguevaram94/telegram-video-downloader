"""Bloqueo compartido por ruta canónica para clasificación de medios.

Coordina la clasificación automática (DownloadManager) y la reclasificación
manual (Inbox) para que un mismo archivo no se clasifique en paralelo.
"""

from __future__ import annotations

import threading
from pathlib import Path


def canonical_path_key(path: Path) -> str:
    """Clave estable por ruta absoluta resuelta (sin exigir que exista)."""
    return str(path.resolve())


class ClassificationPathLock:
    """Exclusión mutua no bloqueante por archivo (try-acquire).

    No usa esperas indefinidas: si la ruta ya está en proceso, ``try_acquire``
    devuelve False de inmediato. El llamador debe liberar siempre con
    ``release`` (idealmente en ``finally``).
    """

    def __init__(self) -> None:
        # threading.Lock evita depender de un event loop en el __init__.
        self._guard = threading.Lock()
        self._processing: set[str] = set()

    def try_acquire(self, path: Path) -> bool:
        key = canonical_path_key(path)
        with self._guard:
            if key in self._processing:
                return False
            self._processing.add(key)
            return True

    def release(self, path: Path) -> None:
        key = canonical_path_key(path)
        with self._guard:
            self._processing.discard(key)

    def is_held(self, path: Path) -> bool:
        key = canonical_path_key(path)
        with self._guard:
            return key in self._processing


_classification_lock = ClassificationPathLock()


def get_classification_lock() -> ClassificationPathLock:
    return _classification_lock
