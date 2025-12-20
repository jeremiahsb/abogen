from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any, Iterable, Iterator, Optional

import numpy as np


DEFAULT_SUPERTONIC_VOICES = ("M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5")


@dataclass
class SupertonicSegment:
    graphemes: str
    audio: np.ndarray


def _ensure_float32_mono(wav: Any) -> np.ndarray:
    arr = np.asarray(wav, dtype="float32")
    if arr.ndim == 2:
        # (n, 1) or (1, n) or (n, channels)
        if arr.shape[0] == 1 and arr.shape[1] > 1:
            arr = arr.reshape(-1)
        else:
            arr = arr[:, 0]
    return arr.reshape(-1)


def _resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return audio
    if audio.size == 0:
        return audio
    ratio = dst_rate / float(src_rate)
    new_len = int(round(audio.size * ratio))
    if new_len <= 1:
        return np.zeros(0, dtype="float32")
    x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
    return np.interp(x_new, x_old, audio).astype("float32", copy=False)


def _split_text(text: str, *, split_pattern: Optional[str], max_chunk_length: int) -> list[str]:
    stripped = (text or "").strip()
    if not stripped:
        return []
    parts: list[str]
    if split_pattern:
        try:
            parts = [p.strip() for p in re.split(split_pattern, stripped) if p.strip()]
        except re.error:
            parts = [stripped]
    else:
        parts = [stripped]

    # Enforce max length by hard-splitting long parts.
    result: list[str] = []
    for part in parts:
        if len(part) <= max_chunk_length:
            result.append(part)
            continue
        start = 0
        while start < len(part):
            end = min(len(part), start + max_chunk_length)
            # Try to split at whitespace.
            if end < len(part):
                ws = part.rfind(" ", start, end)
                if ws > start + 40:
                    end = ws
            chunk = part[start:end].strip()
            if chunk:
                result.append(chunk)
            start = end
    return result


class SupertonicPipeline:
    """Minimal adapter that mimics Kokoro's pipeline iteration interface."""

    def __init__(
        self,
        *,
        sample_rate: int,
        auto_download: bool = True,
        total_steps: int = 5,
        max_chunk_length: int = 300,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.total_steps = int(total_steps)
        self.max_chunk_length = int(max_chunk_length)

        try:
            from supertonic import TTS  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "Supertonic is not installed. Install it with `pip install supertonic`."
            ) from exc

        self._tts = TTS(auto_download=auto_download)

    def __call__(
        self,
        text: str,
        *,
        voice: str,
        speed: float,
        split_pattern: Optional[str] = None,
        total_steps: Optional[int] = None,
    ) -> Iterator[SupertonicSegment]:
        voice_name = (voice or "").strip() or "M1"
        steps = int(total_steps) if total_steps is not None else self.total_steps
        steps = max(2, min(15, steps))
        speed_value = float(speed) if speed is not None else 1.0
        speed_value = max(0.7, min(2.0, speed_value))

        style = self._tts.get_voice_style(voice_name=voice_name)
        chunks = _split_text(text, split_pattern=split_pattern, max_chunk_length=self.max_chunk_length)
        for chunk in chunks:
            wav, duration = self._tts.synthesize(
                text=chunk,
                voice_style=style,
                total_steps=steps,
                speed=speed_value,
                max_chunk_length=self.max_chunk_length,
                silence_duration=0.0,
                verbose=False,
            )
            audio = _ensure_float32_mono(wav)

            # If duration is present, infer the source sample rate and resample if needed.
            src_rate = self.sample_rate
            try:
                dur = float(duration)
                if dur > 0 and audio.size > 0:
                    inferred = int(round(audio.size / dur))
                    if 8000 <= inferred <= 96000:
                        src_rate = inferred
            except Exception:
                pass

            if src_rate != self.sample_rate:
                audio = _resample_linear(audio, src_rate, self.sample_rate)

            yield SupertonicSegment(graphemes=chunk, audio=audio)
