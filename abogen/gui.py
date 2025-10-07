"""Legacy PyQt GUI module removed in favor of the web interface."""

from __future__ import annotations


class abogen:  # pragma: no cover - legacy entry point
    """Placeholder for the removed PyQt GUI class."""

    def __init__(self, *_args, **_kwargs):
        raise RuntimeError(
            "The PyQt desktop interface has been removed. Please use the web UI instead."
        )


__all__ = ["abogen"]
