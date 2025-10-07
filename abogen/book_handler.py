"""Legacy PyQt-based chapter selection dialog has been removed."""

from __future__ import annotations


def __getattr__(name: str):
    raise AttributeError(
        "The PyQt chapter selection dialog was removed. Use the web interface instead."
    )
