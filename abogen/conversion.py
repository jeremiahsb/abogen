"""Legacy PyQt conversion helpers removed."""

from __future__ import annotations


def __getattr__(name: str):  # pragma: no cover - compatibility shim
    raise AttributeError(
        "The PyQt-based conversion helpers were removed. Use the web service pipeline instead."
    )
