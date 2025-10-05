from __future__ import annotations

import json
import math
import re
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import soundfile as sf
import static_ffmpeg

from abogen.text_extractor import ExtractedChapter, extract_from_path
from abogen.utils import (
    calculate_text_length,
    create_process,
    get_user_cache_path,
    load_config,
    load_numpy_kpipeline,
)
from abogen.voice_formulas import get_new_voice

from .service import Job, JobStatus


SPLIT_PATTERN = r"\n+"
SAMPLE_RATE = 24000


class _JobCancelled(Exception):
    """Raised internally to abort a conversion when the client cancels."""


@dataclass
class AudioSink:
    write: Callable[[np.ndarray], None]


def run_conversion_job(job: Job) -> None:
    job.add_log("Preparing conversion pipeline")
    canceller = _make_canceller(job)

    sink_stack = ExitStack()
    subtitle_writer: Optional[SubtitleWriter] = None
    chapter_paths: list[Path] = []
    try:
        pipeline = _load_pipeline(job)
        extraction = extract_from_path(job.stored_path)
        job.metadata_tags = extraction.metadata or {}

        total_characters = extraction.total_characters or calculate_text_length(extraction.combined_text)
        if job.total_characters == 0:
            job.total_characters = total_characters
        job.add_log(f"Total characters: {job.total_characters:,}")

        _apply_newline_policy(extraction.chapters, job.replace_single_newlines)

        base_output_dir = _prepare_output_dir(job)
        project_root, audio_dir, subtitle_dir, metadata_dir = _prepare_project_layout(job, base_output_dir)

        merged_required = job.merge_chapters_at_end or not job.save_chapters_separately
        audio_path: Optional[Path] = None
        audio_sink: Optional[AudioSink] = None
        if merged_required:
            audio_path = _build_output_path(audio_dir, job.original_filename, job.output_format)
            meta_for_sink = job.metadata_tags if job.metadata_tags else None
            audio_sink = _open_audio_sink(audio_path, job, sink_stack, metadata=meta_for_sink)
            subtitle_writer = _create_subtitle_writer(job, audio_path)
            job.result.audio_path = audio_path
            if subtitle_writer:
                job.result.subtitle_paths.append(subtitle_writer.path)

        chapter_dir: Optional[Path] = None
        if job.save_chapters_separately:
            chapter_dir = audio_dir / "chapters"
            chapter_dir.mkdir(parents=True, exist_ok=True)

        voice = _resolve_voice(pipeline, job)
        processed_chars = 0
        subtitle_index = 1
        current_time = 0.0
        total_chapters = len(extraction.chapters)

        for idx, chapter in enumerate(extraction.chapters, start=1):
            canceller()
            job.add_log(f"Processing chapter {idx}/{total_chapters}: {chapter.title}")

            chapter_sink_stack = ExitStack()
            chapter_sink: Optional[AudioSink] = None
            chapter_audio_path: Optional[Path] = None

            if chapter_dir is not None:
                chapter_audio_path = _build_output_path(
                    chapter_dir,
                    f"{Path(job.original_filename).stem}_{_slugify(chapter.title, idx)}",
                    job.separate_chapters_format,
                )
                chapter_sink = _open_audio_sink(
                    chapter_audio_path,
                    job,
                    chapter_sink_stack,
                    fmt=job.separate_chapters_format,
                )

            for segment in pipeline(
                chapter.text,
                voice=voice,
                speed=job.speed,
                split_pattern=SPLIT_PATTERN,
            ):
                canceller()
                graphemes = segment.graphemes.strip()
                if not graphemes:
                    continue

                audio = _to_float32(segment.audio)
                if chapter_sink:
                    chapter_sink.write(audio)
                if audio_sink:
                    audio_sink.write(audio)

                duration = len(audio) / SAMPLE_RATE
                processed_chars += len(graphemes)
                job.processed_characters = processed_chars
                if job.total_characters:
                    job.progress = min(processed_chars / job.total_characters, 0.999)
                job.add_log(f"{processed_chars:,}/{job.total_characters or '—'}: {graphemes[:80]}")

                if subtitle_writer and audio_sink:
                    subtitle_writer.write_segment(
                        index=subtitle_index,
                        text=graphemes,
                        start=current_time,
                        end=current_time + duration,
                    )
                    subtitle_index += 1

                if audio_sink:
                    current_time += duration

            if chapter_sink:
                chapter_sink_stack.close()
                job.result.artifacts[f"chapter_{idx:02d}"] = chapter_audio_path
                chapter_paths.append(chapter_audio_path)

            if (
                audio_sink
                and job.merge_chapters_at_end
                and idx < total_chapters
                and job.silence_between_chapters > 0
            ):
                silence_samples = int(job.silence_between_chapters * SAMPLE_RATE)
                if silence_samples > 0:
                    silence = np.zeros(silence_samples, dtype="float32")
                    audio_sink.write(silence)
                    current_time += job.silence_between_chapters

        if not audio_path and chapter_paths:
            job.result.audio_path = chapter_paths[0]

        if metadata_dir:
            metadata_dir.mkdir(parents=True, exist_ok=True)
            metadata_file = metadata_dir / "metadata.json"
            metadata_file.write_text(json.dumps({"metadata": job.metadata_tags}, indent=2), encoding="utf-8")
            job.result.artifacts["metadata"] = metadata_file

        if job.save_as_project:
            job.result.artifacts["project_root"] = project_root

        if job.status != JobStatus.CANCELLED:
            job.progress = 1.0

    except _JobCancelled:
        job.status = JobStatus.CANCELLED
        job.add_log("Job cancelled", level="warning")
    except Exception as exc:  # pragma: no cover - defensive guard
        job.error = str(exc)
        job.status = JobStatus.FAILED
        job.add_log(f"Job failed: {exc}", level="error")
    finally:
        sink_stack.close()
        if subtitle_writer:
            subtitle_writer.close()


def _load_pipeline(job: Job):
    cfg = load_config()
    disable_gpu = not job.use_gpu or not cfg.get("use_gpu", True)
    device = "cpu"
    if not disable_gpu:
        device = _select_device()
    _np, KPipeline = load_numpy_kpipeline()
    return KPipeline(lang_code=job.language, repo_id="hexgrad/Kokoro-82M", device=device)


def _select_device() -> str:
    import platform

    system = platform.system()
    if system == "Darwin" and platform.processor() == "arm":
        return "mps"
    return "cuda"


def _prepare_output_dir(job: Job) -> Path:
    from platformdirs import user_desktop_dir

    default_output = Path(get_user_cache_path("outputs"))
    if job.save_mode == "Save to Desktop":
        directory = Path(user_desktop_dir())
    elif job.save_mode == "Save next to input file":
        directory = job.stored_path.parent
    elif job.save_mode == "Choose output folder" and job.output_folder:
        directory = Path(job.output_folder)
    else:
        directory = default_output
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _build_output_path(directory: Path, original_name: str, extension: str) -> Path:
    base_name = Path(original_name).stem
    sanitized = re.sub(r"[^\w\-_.]+", "_", base_name).strip("_") or "output"
    candidate = directory / f"{sanitized}.{extension}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{sanitized}_{counter}.{extension}"
        counter += 1
    return candidate


def _prepare_project_layout(job: Job, base_dir: Path) -> tuple[Path, Path, Path, Optional[Path]]:
    base_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(job.original_filename).stem
    if job.save_as_project:
        project_root = _ensure_unique_directory(base_dir, f"{stem}_project")
        audio_dir = project_root / "audio"
        subtitle_dir = project_root / "subtitles"
        metadata_dir = project_root / "metadata"
        for directory in (audio_dir, subtitle_dir, metadata_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return project_root, audio_dir, subtitle_dir, metadata_dir

    return base_dir, base_dir, base_dir, None


def _ensure_unique_directory(parent: Path, name: str) -> Path:
    candidate = parent / name
    counter = 1
    while candidate.exists():
        candidate = parent / f"{name}_{counter}"
        counter += 1
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _apply_newline_policy(chapters: List[ExtractedChapter], replace_single_newlines: bool) -> None:
    if not replace_single_newlines:
        return
    newline_regex = re.compile(r"(?<!\n)\n(?!\n)")
    for chapter in chapters:
        chapter.text = newline_regex.sub(" ", chapter.text)


def _slugify(title: str, index: int) -> str:
    sanitized = re.sub(r"[^\w\-]+", "_", title.lower()).strip("_")
    if not sanitized:
        sanitized = f"chapter_{index:02d}"
    return sanitized[:80]


def _open_audio_sink(
    path: Path,
    job: Job,
    stack: ExitStack,
    *,
    fmt: Optional[str] = None,
    metadata: Optional[Dict[str, str]] = None,
) -> AudioSink:
    static_ffmpeg.add_paths()
    fmt_value = (fmt or job.output_format).lower()

    if fmt_value in {"wav", "flac"}:
        soundfile = stack.enter_context(
            sf.SoundFile(path, mode="w", samplerate=SAMPLE_RATE, channels=1, format=fmt_value.upper())
        )
        return AudioSink(write=lambda data: soundfile.write(data))

    cmd = _build_ffmpeg_command(path, fmt_value, metadata=metadata)
    process = create_process(cmd, stdin=subprocess.PIPE, text=False)

    def _finalize() -> None:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.wait()

    stack.callback(_finalize)

    def _write(data: np.ndarray) -> None:
        if job.cancel_requested or process.stdin is None:
            return
        process.stdin.write(data.tobytes())

    return AudioSink(write=_write)


def _build_ffmpeg_command(path: Path, fmt: str, metadata: Optional[Dict[str, str]] = None) -> list[str]:
    base = [
        "ffmpeg",
        "-y",
        "-f",
        "f32le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-i",
        "pipe:0",
    ]
    if fmt == "mp3":
        base += ["-c:a", "libmp3lame", "-qscale:a", "2"]
    elif fmt == "opus":
        base += ["-c:a", "libopus", "-b:a", "24000"]
    elif fmt == "m4b":
        base += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart+use_metadata_tags"]
    else:
        base += ["-c:a", "copy"]

    if metadata:
        for key, value in metadata.items():
            if value:
                base += ["-metadata", f"{key}={value}"]
    base.append(str(path))
    return base


def _resolve_voice(pipeline, job: Job):
    if "*" in job.voice:
        return get_new_voice(pipeline, job.voice, job.use_gpu)
    return job.voice


def _to_float32(audio_segment) -> np.ndarray:
    if hasattr(audio_segment, "numpy"):
        return audio_segment.numpy().astype("float32")
    return np.asarray(audio_segment, dtype="float32")


class SubtitleWriter:
    def __init__(self, path: Path, format_key: str) -> None:
        self.path = path
        self.format_key = format_key
        self._file = path.open("w", encoding="utf-8", errors="replace")
        if format_key == "ass":
            self._write_ass_header()

    def write_segment(self, *, index: int, text: str, start: float, end: float) -> None:
        if self.format_key == "ass":
            self._write_ass_event(text, start, end)
        else:
            self._write_srt_line(index, text, start, end)

    def close(self) -> None:
        self._file.close()

    def _write_srt_line(self, index: int, text: str, start: float, end: float) -> None:
        self._file.write(f"{index}\n")
        self._file.write(f"{_format_timestamp(start)} --> {_format_timestamp(end)}\n")
        self._file.write(text.strip() + "\n\n")

    def _write_ass_header(self) -> None:
        self._file.write("[Script Info]\n")
        self._file.write("Title: Generated by Abogen\n")
        self._file.write("ScriptType: v4.00+\n\n")
        self._file.write("[V4+ Styles]\n")
        self._file.write(
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        )
        self._file.write(
            "Style: Default,Arial,24,&H00FFFFFF,&H00808080,&H00000000,&H00404040,0,0,0,0,100,100,0,0,3,2,0,5,10,10,10,1\n\n"
        )
        self._file.write("[Events]\n")
        self._file.write(
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

    def _write_ass_event(self, text: str, start: float, end: float) -> None:
        self._file.write(
            f"Dialogue: 0,{_format_timestamp(start, ass=True)},{_format_timestamp(end, ass=True)},Default,,0000,0000,0000,,{text.strip()}\n"
        )


def _create_subtitle_writer(job: Job, audio_path: Path) -> Optional[SubtitleWriter]:
    if job.subtitle_mode == "Disabled":
        return None

    fmt = (job.subtitle_format or "srt").lower()
    if job.subtitle_mode == "Sentence + Highlighting" and fmt == "srt":
        job.add_log("Highlighting requires ASS subtitles. Switching format.", level="warning")
        fmt = "ass"

    if fmt == "srt":
        return SubtitleWriter(audio_path.with_suffix(".srt"), "srt")
    if "ass" in fmt:
        return SubtitleWriter(audio_path.with_suffix(".ass"), "ass")

    job.add_log(f"Unsupported subtitle format '{job.subtitle_format}'. Skipping.", level="warning")
    return None


def _format_timestamp(value: float, ass: bool = False) -> str:
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = int(value % 60)
    milliseconds = int((value - math.floor(value)) * 1000)
    if ass:
        centiseconds = int(milliseconds / 10)
        return f"{hours:d}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _make_canceller(job: Job) -> Callable[[], None]:
    def _cancel() -> None:
        if job.cancel_requested:
            raise _JobCancelled

    return _cancel
