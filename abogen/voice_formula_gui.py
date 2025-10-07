"""Legacy PyQt voice formula dialog removed."""

from __future__ import annotations


class VoiceFormulaDialog:  # pragma: no cover - legacy entry point
    """Placeholder for removed PyQt dialog."""

    def __init__(self, *_args, **_kwargs):
        raise RuntimeError(
            "The PyQt-based voice formula editor has been removed. Use the web tools instead."
        )


__all__ = ["VoiceFormulaDialog"]
