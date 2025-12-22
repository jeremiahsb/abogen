"""Backwards-compatible re-export of the PyQt book handler.

The actual implementation lives in abogen.pyqt.book_handler.
"""

from __future__ import annotations

from abogen.pyqt.book_handler import *  # noqa: F401, F403
from abogen.pyqt.book_handler import HandlerDialog

__all__ = ["HandlerDialog"]
