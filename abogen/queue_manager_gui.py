"""Legacy PyQt queue manager GUI removed."""

from __future__ import annotations


def __getattr__(name: str):  # pragma: no cover - compatibility shim
    raise AttributeError(
        "The PyQt queue manager GUI has been removed. Use the web dashboard instead."
    )
