"""Cola de descargas manuales solicitadas desde el panel web."""

from __future__ import annotations

import fcntl
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManualDownloadRequest:
    message_id: int
    replace: bool


def _lock_path(queue_file: Path) -> Path:
    return queue_file.with_suffix(".lock")


def _read_queue_unlocked(queue_file: Path) -> list[ManualDownloadRequest]:
    if not queue_file.exists():
        return []

    try:
        raw = json.loads(queue_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(raw, list):
        return []

    requests: list[ManualDownloadRequest] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        message_id = item.get("message_id")
        if not isinstance(message_id, int):
            continue
        replace = bool(item.get("replace", False))
        requests.append(ManualDownloadRequest(message_id=message_id, replace=replace))

    return requests


def _write_queue_unlocked(
    queue_file: Path,
    requests: list[ManualDownloadRequest],
) -> None:
    payload = [
        {"message_id": request.message_id, "replace": request.replace}
        for request in requests
    ]
    queue_file.write_text(json.dumps(payload), encoding="utf-8")


def _with_queue_lock(queue_file: Path, action):
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(queue_file)
    with lock_file.open("w", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        return action()


def load_queue(queue_file: Path) -> list[ManualDownloadRequest]:
    return _with_queue_lock(queue_file, lambda: _read_queue_unlocked(queue_file))


def append_requests(
    queue_file: Path,
    requests: list[ManualDownloadRequest],
) -> None:
    if not requests:
        return

    def action() -> None:
        existing = _read_queue_unlocked(queue_file)
        pending_ids = {request.message_id for request in existing}
        merged = list(existing)
        for request in requests:
            if request.message_id in pending_ids:
                continue
            merged.append(request)
            pending_ids.add(request.message_id)
        _write_queue_unlocked(queue_file, merged)

    _with_queue_lock(queue_file, action)


def take_pending(queue_file: Path) -> list[ManualDownloadRequest]:
    def action() -> list[ManualDownloadRequest]:
        pending = _read_queue_unlocked(queue_file)
        _write_queue_unlocked(queue_file, [])
        return pending

    return _with_queue_lock(queue_file, action)
