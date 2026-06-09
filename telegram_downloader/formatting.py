"""Formateo de tamaños de archivo para salida legible."""

from __future__ import annotations


def format_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "tamaño desconocido"
    if num_bytes >= 1024**3:
        return f"{num_bytes / 1024**3:.2f} GB"
    return f"{num_bytes / 1024**2:.1f} MB"
