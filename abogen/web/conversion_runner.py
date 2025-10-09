from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import traceback
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, cast

import numpy as np
import soundfile as sf
import static_ffmpeg

from abogen.constants import VOICES_INTERNAL
from abogen.epub3.exporter import build_epub3_package
from abogen.kokoro_text_normalization import (
    ApostropheConfig,
    apply_phoneme_hints,
    expand_titles_and_suffixes,
    ensure_terminal_punctuation,
    normalize_apostrophes,
)
from abogen.text_extractor import ExtractedChapter, extract_from_path
from abogen.utils import (
    calculate_text_length,
    create_process,
    get_internal_cache_path,
    get_user_cache_path,
    get_user_output_path,
    load_config,
    load_numpy_kpipeline,
)
from abogen.voice_cache import ensure_voice_assets
from abogen.voice_formulas import extract_voice_ids, get_new_voice

from .service import Job, JobStatus


SPLIT_PATTERN = r"\n+"
SAMPLE_RATE = 24000


class _JobCancelled(Exception):
    """Raised internally to abort a conversion when the client cancels."""


@dataclass
class AudioSink:
    write: Callable[[np.ndarray], None]


def _coerce_truthy(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    return bool(value)


def _spec_to_voice_ids(spec: Any) -> Set[str]:
    text = str(spec or "").strip()
    if not text:
        return set()
    if "*" in text:
        try:
            return set(extract_voice_ids(text))
        except ValueError:
            return set()
    if text in VOICES_INTERNAL:
        return {text}
    return set()


def _collect_required_voice_ids(job: Job) -> Set[str]:
    voices: Set[str] = set()
    voices.update(_spec_to_voice_ids(job.voice))

    for chapter in getattr(job, "chapters", []) or []:
        if not isinstance(chapter, dict):
            continue
        for key in ("resolved_voice", "voice_formula", "voice"):
            voices.update(_spec_to_voice_ids(chapter.get(key)))

    for chunk in getattr(job, "chunks", []) or []:
        if not isinstance(chunk, dict):
            continue
        for key in ("resolved_voice", "voice_formula", "voice"):
            voices.update(_spec_to_voice_ids(chunk.get(key)))

    speakers = getattr(job, "speakers", {})
    if isinstance(speakers, dict):
        for payload in speakers.values() or []:
            if not isinstance(payload, dict):
                continue
            for key in ("resolved_voice", "voice_formula", "voice"):
                voices.update(_spec_to_voice_ids(payload.get(key)))

    voices.update(VOICES_INTERNAL)
    return voices


def _initialize_voice_cache(job: Job) -> None:
    try:
        targets = _collect_required_voice_ids(job)
        downloaded, errors = ensure_voice_assets(
            targets,
            on_progress=lambda message: job.add_log(message, level="debug"),
        )
    except RuntimeError as exc:
        job.add_log(f"Voice cache unavailable: {exc}", level="warning")
        return

    if downloaded:
        job.add_log(
            f"Cached {len(downloaded)} voice asset{'s' if len(downloaded) != 1 else ''} locally.",
            level="info",
        )

    for voice_id, error in errors.items():
        job.add_log(f"Failed to cache voice '{voice_id}': {error}", level="warning")


_SIGNIFICANT_LENGTH_THRESHOLDS: Dict[str, int] = {"epub": 1000, "markdown": 500}
_MIN_SHORT_CONTENT: Dict[str, int] = {"epub": 240, "markdown": 160}
_STRUCTURAL_KEYWORDS = (
    "preface",
    "prologue",
    "introduction",
    "foreword",
    "epilogue",
    "afterword",
    "appendix",
    "acknowledgment",
    "acknowledgement",
)
_STRUCTURAL_MIN_LENGTH = 120
_MAX_SHORT_CHAPTERS = 2


def _infer_file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return "epub"
    if suffix in {".md", ".markdown"}:
        return "markdown"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".txt":
        return "text"
    return suffix.lstrip(".") or "text"


def _looks_structural(title: str) -> bool:
    lowered = title.strip().lower()
    if not lowered:
        return False
    return any(keyword in lowered for keyword in _STRUCTURAL_KEYWORDS)


def _auto_select_relevant_chapters(
    chapters: List[ExtractedChapter],
    file_type: str,
) -> tuple[List[ExtractedChapter], List[tuple[str, int]]]:
    if not chapters:
        return [], []

    normalized = file_type.lower()
    threshold = _SIGNIFICANT_LENGTH_THRESHOLDS.get(normalized, 0)
    min_short = _MIN_SHORT_CONTENT.get(normalized, 0)

    kept: List[ExtractedChapter] = []
    skipped: List[tuple[str, int]] = []
    short_kept = 0

    for chapter in chapters:
        stripped = chapter.text.strip()
        length = len(stripped)
        if length == 0:
            skipped.append((chapter.title, length))
            continue

        keep = False
        if threshold == 0:
            keep = True
        elif length >= threshold:
            keep = True
        elif not kept:
            keep = True
        elif min_short and length >= min_short and short_kept < _MAX_SHORT_CHAPTERS:
            keep = True
            short_kept += 1
        elif _looks_structural(chapter.title) and length >= _STRUCTURAL_MIN_LENGTH:
            keep = True

        if keep:
            kept.append(chapter)
        else:
            skipped.append((chapter.title, length))

    if kept:
        return kept, skipped

    # Fallback: retain the longest non-empty chapter so conversion can proceed.
    longest_idx = None
    longest_length = 0
    for idx, chapter in enumerate(chapters):
        stripped_length = len(chapter.text.strip())
        if stripped_length > longest_length:
            longest_length = stripped_length
            longest_idx = idx

    if longest_idx is None or longest_length == 0:
        return [], []

    fallback_chapter = chapters[longest_idx]
    kept = [fallback_chapter]
    skipped = [
        (chapter.title, len(chapter.text.strip()))
        for idx, chapter in enumerate(chapters)
        if idx != longest_idx and chapter.text.strip()
    ]
    return kept, skipped


def _chapter_label(file_type: str) -> str:
    return "chapters" if file_type.lower() in {"epub", "markdown"} else "pages"


def _update_metadata_for_chapter_count(metadata: Dict[str, Any], count: int, file_type: str) -> None:
    if not metadata or count <= 0:
        return

    label = "Chapters" if file_type.lower() in {"epub", "markdown"} else "Pages"
    metadata["chapter_count"] = str(count)

    pattern = re.compile(r"\(\d+\s+(Chapters?|Pages?)\)")
    replacement = f"({count} {label})"
    for key in ("album", "ALBUM"):
        value = metadata.get(key)
        if not isinstance(value, str):
            continue
        metadata[key] = pattern.sub(replacement, value)


def _apply_chapter_overrides(
    extracted: List[ExtractedChapter],
    overrides: List[Dict[str, Any]],
) -> tuple[List[ExtractedChapter], Dict[str, str], List[str]]:
    if not overrides:
        return [], {}, []

    selected: List[ExtractedChapter] = []
    metadata_updates: Dict[str, str] = {}
    diagnostics: List[str] = []

    for position, payload in enumerate(overrides):
        if not isinstance(payload, dict):
            diagnostics.append(
                f"Skipped chapter override at position {position + 1}: unsupported payload type {type(payload).__name__}."
            )
            continue

        enabled = _coerce_truthy(payload.get("enabled", True))
        payload["enabled"] = enabled
        if not enabled:
            continue

        metadata_payload = payload.get("metadata") or {}
        if isinstance(metadata_payload, dict):
            for key, value in metadata_payload.items():
                if value is None:
                    continue
                metadata_updates[str(key)] = str(value)

        base: Optional[ExtractedChapter] = None
        idx_candidate = payload.get("index")
        idx_normalized: Optional[int] = None
        if isinstance(idx_candidate, int):
            idx_normalized = idx_candidate
        elif isinstance(idx_candidate, str):
            try:
                idx_normalized = int(idx_candidate)
            except ValueError:
                idx_normalized = None
        if idx_normalized is not None and 0 <= idx_normalized < len(extracted):
            base = extracted[idx_normalized]
            payload["index"] = idx_normalized

        if base is None:
            source_title = payload.get("source_title")
            if isinstance(source_title, str):
                base = next((chapter for chapter in extracted if chapter.title == source_title), None)

        if base is None:
            candidate_title = payload.get("title")
            if isinstance(candidate_title, str):
                base = next((chapter for chapter in extracted if chapter.title == candidate_title), None)

        text_override = payload.get("text")
        if text_override is not None:
            text_value = str(text_override)
        elif base is not None:
            text_value = base.text
        else:
            diagnostics.append(
                f"Skipped chapter override at position {position + 1}: no text provided and no matching source chapter found."
            )
            continue

        title_override = payload.get("title")
        if title_override is not None:
            title_value = str(title_override)
        elif base is not None:
            title_value = base.title
        else:
            title_value = f"Chapter {position + 1}"

        if base and not payload.get("source_title"):
            payload["source_title"] = base.title

        payload["title"] = title_value
        payload["text"] = text_value
        payload["characters"] = len(text_value)
        payload.setdefault("order", payload.get("order", position))

        selected.append(ExtractedChapter(title=title_value, text=text_value))

    return selected, metadata_updates, diagnostics


def _merge_metadata(
    extracted: Optional[Dict[str, str]],
    overrides: Dict[str, Any],
) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    if extracted:
        for key, value in extracted.items():
            if value is None:
                continue
            merged[str(key)] = str(value)
    for key, value in (overrides or {}).items():
        key_str = str(key)
        if value is None:
            merged.pop(key_str, None)
        else:
            merged[key_str] = str(value)
    return merged


_APOSTROPHE_CONFIG = ApostropheConfig()


def _normalize_for_pipeline(text: str) -> str:
    normalized, _details = normalize_apostrophes(text, _APOSTROPHE_CONFIG)
    normalized = expand_titles_and_suffixes(normalized)
    normalized = ensure_terminal_punctuation(normalized)
    if _APOSTROPHE_CONFIG.add_phoneme_hints:
        return apply_phoneme_hints(normalized, iz_marker=_APOSTROPHE_CONFIG.sibilant_iz_marker)
    return normalized


def _chapter_voice_spec(job: Job, override: Optional[Dict[str, Any]]) -> str:
    if not override:
        return job.voice or ""

    resolved = str(override.get("resolved_voice", "")).strip()
    if resolved:
        return resolved

    formula = str(override.get("voice_formula", "")).strip()
    if formula:
        return formula

    voice = str(override.get("voice", "")).strip()
    if voice:
        return voice

    return job.voice or ""


def _chunk_voice_spec(job: Any, chunk: Dict[str, Any], fallback: str) -> str:
    for key in ("resolved_voice", "voice_formula", "voice"):
        value = chunk.get(key)
        if value:
            return str(value)

    speaker_id = chunk.get("speaker_id")
    speakers = getattr(job, "speakers", None)
    if isinstance(speakers, dict) and speaker_id in speakers:
        speaker_entry = speakers.get(speaker_id) or {}
        if isinstance(speaker_entry, dict):
            for key in ("resolved_voice", "voice_formula", "voice"):
                value = speaker_entry.get(key)
                if value:
                    return str(value)
            profile_formula = speaker_entry.get("voice_formula")
            if profile_formula:
                return str(profile_formula)

    profile_name = chunk.get("voice_profile")
    if profile_name:
        if isinstance(speakers, dict):
            speaker_entry = speakers.get(profile_name)
            if isinstance(speaker_entry, dict):
                for key in ("resolved_voice", "voice_formula", "voice"):
                    value = speaker_entry.get(key)
                    if value:
                        return str(value)

    return fallback or getattr(job, "voice", "") or ""


def _group_chunks_by_chapter(chunks: Iterable[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for entry in chunks or []:
        if not isinstance(entry, dict):
            continue
        try:
            chapter_index = int(entry.get("chapter_index", 0))
        except (TypeError, ValueError):
            chapter_index = 0
        grouped[chapter_index].append(dict(entry))

    for chapter_index, items in grouped.items():
        items.sort(key=lambda payload: _safe_int(payload.get("chunk_index")))

    return grouped


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _escape_ffmetadata_value(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace("\n", "\\n")
    escaped = escaped.replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
    return escaped


def _metadata_to_ffmpeg_args(metadata: Dict[str, Any]) -> List[str]:
    args: List[str] = []
    for key, value in (metadata or {}).items():
        if value in (None, ""):
            continue
        key_str = str(key).strip()
        if not key_str:
            continue
        normalized_key = key_str.lower()
        if normalized_key == "year":
            ffmpeg_key = "date"
        else:
            ffmpeg_key = key_str
        args.extend(["-metadata", f"{ffmpeg_key}={value}"])
    return args


def _render_ffmetadata(metadata: Dict[str, Any], chapters: List[Dict[str, Any]]) -> str:
    lines: List[str] = [";FFMETADATA1"]
    for key, value in (metadata or {}).items():
        if value is None:
            continue
        key_str = str(key).strip()
        if not key_str:
            continue
        lines.append(f"{key_str}={_escape_ffmetadata_value(value)}")

    for chapter in chapters or []:
        start = chapter.get("start")
        end = chapter.get("end")
        if start is None or end is None:
            continue
        try:
            start_ms = max(0, int(round(float(start) * 1000)))
            end_ms = int(round(float(end) * 1000))
        except (TypeError, ValueError):
            continue
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        title = chapter.get("title")
        if title:
            lines.append(f"title={_escape_ffmetadata_value(title)}")
        voice = chapter.get("voice")
        if voice:
            lines.append(f"voice={_escape_ffmetadata_value(voice)}")

    return "\n".join(lines) + "\n"


def _write_ffmetadata_file(
    audio_path: Path,
    metadata: Dict[str, Any],
    chapters: List[Dict[str, Any]],
) -> Optional[Path]:
    content = _render_ffmetadata(metadata, chapters)
    if content.strip() == ";FFMETADATA1":
        return None
    directory = audio_path.parent if audio_path.parent.exists() else Path(tempfile.gettempdir())
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".ffmeta",
        delete=False,
        dir=str(directory),
    ) as handle:
        handle.write(content)
        return Path(handle.name)


def _apply_m4b_chapters_with_mutagen(
    audio_path: Path,
    chapters: List[Dict[str, Any]],
    job: Job,
) -> bool:
    if not chapters:
        return False

    try:
        from fractions import Fraction
        from mutagen.mp4 import MP4, MP4Chapter  # type: ignore[import]
    except ImportError:
        job.add_log(
            "Unable to write MP4 chapter atoms because mutagen is not installed.",
            level="warning",
        )
        return False

    try:
        mp4 = MP4(str(audio_path))
    except Exception as exc:  # pragma: no cover - defensive
        job.add_log(f"Failed to open m4b for chapter embedding: {exc}", level="warning")
        return False

    chapter_objects: List[MP4Chapter] = []
    for index, entry in enumerate(sorted(chapters, key=lambda item: float(item.get("start") or 0.0))):
        start_raw = entry.get("start")
        if start_raw is None:
            continue
        try:
            start_seconds = max(0.0, float(start_raw))
        except (TypeError, ValueError):
            continue

        title_value = entry.get("title")
        title_text = str(title_value) if title_value else f"Chapter {index + 1}"

        start_fraction = Fraction(int(round(start_seconds * 1000)), 1000)
        chapter_atom = MP4Chapter(start_fraction, title_text)

        end_raw = entry.get("end")
        if end_raw is not None:
            try:
                end_seconds = float(end_raw)
            except (TypeError, ValueError):
                end_seconds = None
            if end_seconds is not None and end_seconds > start_seconds:
                chapter_atom.end = Fraction(int(round(end_seconds * 1000)), 1000)

        chapter_objects.append(chapter_atom)

    if not chapter_objects:
        return False

    try:
        mp4.chapters = cast(Any, chapter_objects)
        mp4.save()
    except Exception as exc:  # pragma: no cover - defensive
        job.add_log(f"Failed to persist MP4 chapter atoms: {exc}", level="warning")
        return False

    return True


def _embed_m4b_metadata(
    audio_path: Path,
    metadata_payload: Dict[str, Any],
    job: Job,
) -> None:
    metadata_map = dict(metadata_payload.get("metadata") or {})
    chapter_entries = list(metadata_payload.get("chapters") or [])
    ffmetadata_path = _write_ffmetadata_file(audio_path, metadata_map, chapter_entries)
    cover_path: Optional[Path] = None
    if job.cover_image_path:
        candidate = Path(job.cover_image_path)
        if candidate.exists():
            cover_path = candidate

    metadata_args = _metadata_to_ffmpeg_args(metadata_map)

    if not ffmetadata_path and not cover_path and not metadata_args:
        return

    job.add_log("Embedding metadata into m4b output")

    command: List[str] = ["ffmpeg", "-y", "-i", str(audio_path)]
    metadata_index: Optional[int] = None
    cover_index: Optional[int] = None
    next_index = 1

    if ffmetadata_path:
        command += ["-f", "ffmetadata", "-i", str(ffmetadata_path)]
        metadata_index = next_index
        next_index += 1

    if cover_path:
        command += ["-i", str(cover_path)]
        cover_index = next_index
        next_index += 1

    command += ["-map", "0:a"]
    command += ["-c:a", "copy"]

    if cover_index is not None:
        command += ["-map", f"{cover_index}:v:0"]
        command += ["-c:v:0", "mjpeg"]
        command += ["-disposition:v:0", "attached_pic"]
        command += ["-metadata:s:v:0", "title=Cover Art"]
        if job.cover_image_mime:
            command += ["-metadata:s:v:0", f"mimetype={job.cover_image_mime}"]

    if metadata_index is not None:
        command += ["-map_metadata", str(metadata_index)]
        command += ["-map_chapters", str(metadata_index)]
    else:
        command += ["-map_metadata", "0"]

    if metadata_args:
        command.extend(metadata_args)

    command += ["-movflags", "+faststart+use_metadata_tags"]

    temp_output = audio_path.with_suffix(audio_path.suffix + ".tmp")
    if audio_path.suffix.lower() in {".m4b", ".mp4", ".m4a"}:
        command += ["-f", "mp4"]
    command.append(str(temp_output))

    process = create_process(command, text=True)
    try:
        return_code = process.wait()
    finally:
        if ffmetadata_path and ffmetadata_path.exists():
            try:
                ffmetadata_path.unlink()
            except OSError:
                pass

    if return_code != 0:
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed to embed metadata (exit code {return_code})")

    temp_output.replace(audio_path)
    job.add_log("Embedded metadata and chapters into m4b output", level="info")

    mutagen_applied = _apply_m4b_chapters_with_mutagen(audio_path, chapter_entries, job)
    if mutagen_applied:
        job.add_log(
            f"Applied {len(chapter_entries)} chapter markers via mutagen", level="info"
        )


def run_conversion_job(job: Job) -> None:
    job.add_log("Preparing conversion pipeline")
    canceller = _make_canceller(job)

    sink_stack = ExitStack()
    subtitle_writer: Optional[SubtitleWriter] = None
    chapter_paths: list[Path] = []
    chapter_markers: List[Dict[str, Any]] = []
    chunk_markers: List[Dict[str, Any]] = []
    metadata_payload: Dict[str, Any] = {}
    audio_output_path: Optional[Path] = None
    extraction: Optional[Any] = None
    pipeline: Any = None
    chunk_groups: Dict[int, List[Dict[str, Any]]] = {}
    active_chapter_configs: List[Dict[str, Any]] = []
    try:
        pipeline = _load_pipeline(job)
        _initialize_voice_cache(job)
        extraction = extract_from_path(job.stored_path)
        file_type = _infer_file_type(job.stored_path)

        if not job.chapters:
            filtered, skipped_info = _auto_select_relevant_chapters(extraction.chapters, file_type)
            original_count = len(extraction.chapters)
            if filtered and len(filtered) < original_count:
                extraction.chapters = filtered
                _update_metadata_for_chapter_count(extraction.metadata, len(filtered), file_type)
                threshold = _SIGNIFICANT_LENGTH_THRESHOLDS.get(file_type.lower())
                label = _chapter_label(file_type)
                qualifier = f" (< {threshold} characters)" if threshold else ""
                job.add_log(
                    f"Auto-selected {len(filtered)} of {original_count} {label} based on content{qualifier}.",
                    level="info",
                )
                if skipped_info:
                    preview_count = 5
                    preview = ", ".join(
                        f"{title or 'Untitled'} ({length})" for title, length in skipped_info[:preview_count]
                    )
                    if len(skipped_info) > preview_count:
                        preview += ", …"
                    job.add_log(
                        f"Skipped {len(skipped_info)} short {label}: {preview}",
                        level="debug",
                    )
            elif not filtered:
                job.add_log(
                    "Auto-selection did not identify usable chapters; retaining original set.",
                    level="warning",
                )

        metadata_overrides: Dict[str, Any] = dict(job.metadata_tags or {})
        if job.chapters:
            selected_chapters, chapter_metadata, diagnostics = _apply_chapter_overrides(
                extraction.chapters,
                job.chapters,
            )
            for message in diagnostics:
                job.add_log(message, level="warning")
            if selected_chapters:
                extraction.chapters = selected_chapters
                metadata_overrides.update(chapter_metadata)
                job.add_log(
                    f"Chapter overrides applied: {len(selected_chapters)} selected.",
                    level="info",
                )
                active_chapter_configs = [
                    entry for entry in job.chapters if _coerce_truthy(entry.get("enabled", True))
                ][: len(selected_chapters)]
                if job.chunks:
                    chunk_groups = _group_chunks_by_chapter(job.chunks)
            else:
                raise ValueError("No chapters were enabled in the requested job.")
        elif job.chunks:
            chunk_groups = _group_chunks_by_chapter(job.chunks)

        job.metadata_tags = _merge_metadata(extraction.metadata, metadata_overrides)

        total_characters = extraction.total_characters or calculate_text_length(extraction.combined_text)
        job.total_characters = total_characters
        job.add_log(f"Total characters: {job.total_characters:,}")

        _apply_newline_policy(extraction.chapters, job.replace_single_newlines)

        base_output_dir = _prepare_output_dir(job)
        project_root, audio_dir, subtitle_dir, metadata_dir = _prepare_project_layout(job, base_output_dir)

        if job.output_format.lower() == "m4b" and not job.merge_chapters_at_end:
            job.add_log(
                "Forcing merged output for m4b format; ignoring 'merge chapters at end' setting.",
                level="warning",
            )
            job.merge_chapters_at_end = True

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

        base_voice_spec = (job.voice or "").strip()
        voice_cache: Dict[str, Any] = {}
        if base_voice_spec and "*" not in base_voice_spec:
            voice_cache[base_voice_spec] = _resolve_voice(pipeline, base_voice_spec, job.use_gpu)
        processed_chars = 0
        subtitle_index = 1
        current_time = 0.0
        total_chapters = len(extraction.chapters)
        if chunk_groups:
            chunk_groups = {
                idx: items for idx, items in chunk_groups.items() if 0 <= idx < total_chapters
            }
        job.add_log(f"Detected {total_chapters} chapter{'s' if total_chapters != 1 else ''}")

        def emit_text(
            text: str,
            *,
            voice_choice: Any,
            chapter_sink: Optional[AudioSink],
            preview_prefix: Optional[str] = None,
            split_pattern: Optional[str] = SPLIT_PATTERN,
        ) -> int:
            nonlocal processed_chars, subtitle_index, current_time
            normalized = _normalize_for_pipeline(text)
            local_segments = 0

            for segment in pipeline(
                normalized,
                voice=voice_choice,
                speed=job.speed,
                split_pattern=split_pattern,
            ):
                canceller()
                graphemes_raw = getattr(segment, "graphemes", "") or ""
                graphemes = graphemes_raw.strip()

                audio = _to_float32(getattr(segment, "audio", None))
                if audio.size == 0:
                    continue

                local_segments += 1
                if chapter_sink:
                    chapter_sink.write(audio)
                if audio_sink:
                    audio_sink.write(audio)

                duration = len(audio) / SAMPLE_RATE
                processed_chars += len(graphemes)
                job.processed_characters = processed_chars
                if job.total_characters:
                    job.progress = min(processed_chars / job.total_characters, 0.999)
                else:
                    job.progress = 0.0 if processed_chars == 0 else 0.999

                preview_text = graphemes or (graphemes_raw[:80] if graphemes_raw else "[silence]")
                prefix = f"{preview_prefix} · " if preview_prefix else ""
                job.add_log(f"{prefix}{processed_chars:,}/{job.total_characters or '—'}: {preview_text[:80]}")

                if subtitle_writer and audio_sink and graphemes:
                    subtitle_writer.write_segment(
                        index=subtitle_index,
                        text=graphemes,
                        start=current_time,
                        end=current_time + duration,
                    )
                    subtitle_index += 1

                if audio_sink:
                    current_time += duration

            return local_segments

        def append_silence(
            duration_seconds: float,
            *,
            include_in_chapter: bool,
            chapter_sink: Optional[AudioSink],
        ) -> None:
            nonlocal current_time
            if duration_seconds <= 0:
                return
            samples = int(round(duration_seconds * SAMPLE_RATE))
            if samples <= 0:
                return
            silence = np.zeros(samples, dtype="float32")
            if include_in_chapter and chapter_sink:
                chapter_sink.write(silence)
            if audio_sink:
                audio_sink.write(silence)
                current_time += duration_seconds

        for idx, chapter in enumerate(extraction.chapters, start=1):
            canceller()
            job.add_log(f"Processing chapter {idx}/{total_chapters}: {chapter.title}")

            chapter_start_time = current_time
            chapter_override = (
                active_chapter_configs[idx - 1] if idx - 1 < len(active_chapter_configs) else None
            )
            chapter_voice_spec = _chapter_voice_spec(job, chapter_override)
            if not chapter_voice_spec:
                chapter_voice_spec = base_voice_spec

            voice_choice = voice_cache.get(chapter_voice_spec)
            if voice_choice is None:
                voice_choice = _resolve_voice(pipeline, chapter_voice_spec, job.use_gpu)
                voice_cache[chapter_voice_spec] = voice_choice

            chapter_audio_path: Optional[Path] = None
            segments_emitted = 0

            with ExitStack() as chapter_sink_stack:
                chapter_sink: Optional[AudioSink] = None

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

                speak_heading = bool(chapter.title.strip())
                if speak_heading:
                    stripped_title = chapter.title.strip()
                    if stripped_title:
                        first_line = next((line.strip() for line in chapter.text.splitlines() if line.strip()), "")
                        if first_line and first_line.casefold() == stripped_title.casefold():
                            speak_heading = False

                if speak_heading:
                    heading_segments = emit_text(
                        chapter.title,
                        voice_choice=voice_choice,
                        chapter_sink=chapter_sink,
                        preview_prefix=f"Chapter {idx} title",
                        split_pattern=SPLIT_PATTERN,
                    )
                    segments_emitted += heading_segments
                    if heading_segments > 0 and job.chapter_intro_delay > 0:
                        append_silence(
                            job.chapter_intro_delay,
                            include_in_chapter=True,
                            chapter_sink=chapter_sink,
                        )

                chunks_for_chapter = chunk_groups.get(idx - 1, []) if chunk_groups else []
                body_segments = 0
                if chunks_for_chapter:
                    job.add_log(
                        f"Emitting {len(chunks_for_chapter)} {job.chunk_level} chunks for chapter {idx}.",
                        level="debug",
                    )
                for chunk_entry in chunks_for_chapter:
                    chunk_text = str(chunk_entry.get("text") or "").strip()
                    if not chunk_text:
                        continue

                    chunk_voice_spec = _chunk_voice_spec(
                        job,
                        chunk_entry,
                        chapter_voice_spec or base_voice_spec,
                    )
                    if not chunk_voice_spec:
                        chunk_voice_spec = chapter_voice_spec or base_voice_spec

                    if chunk_voice_spec == chapter_voice_spec:
                        chunk_voice_choice = voice_choice
                    else:
                        chunk_voice_choice = voice_cache.get(chunk_voice_spec)
                        if chunk_voice_choice is None:
                            chunk_voice_choice = _resolve_voice(
                                pipeline,
                                chunk_voice_spec,
                                job.use_gpu,
                            )
                            voice_cache[chunk_voice_spec] = chunk_voice_choice

                    chunk_start = current_time
                    emitted = emit_text(
                        chunk_text,
                        voice_choice=chunk_voice_choice,
                        chapter_sink=chapter_sink,
                        preview_prefix=f"Chunk {chunk_entry.get('id') or chunk_entry.get('chunk_index')}",
                    )
                    if emitted <= 0:
                        continue

                    body_segments += emitted
                    segments_emitted += emitted
                    chunk_markers.append(
                        {
                            "id": chunk_entry.get("id"),
                            "chapter_index": idx - 1,
                            "chunk_index": _safe_int(
                                chunk_entry.get("chunk_index"), len(chunk_markers)
                            ),
                            "start": chunk_start,
                            "end": current_time,
                            "speaker_id": chunk_entry.get("speaker_id", "narrator"),
                            "voice": chunk_voice_spec,
                            "level": chunk_entry.get("level", job.chunk_level),
                            "characters": len(chunk_text),
                        }
                    )

                if body_segments == 0:
                    chapter_body_start = current_time
                    emitted = emit_text(
                        chapter.text,
                        voice_choice=voice_choice,
                        chapter_sink=chapter_sink,
                    )
                    if emitted > 0:
                        segments_emitted += emitted
                        chunk_markers.append(
                            {
                                "id": None,
                                "chapter_index": idx - 1,
                                "chunk_index": 0,
                                "start": chapter_body_start,
                                "end": current_time,
                                "speaker_id": "narrator",
                                "voice": chapter_voice_spec,
                                "level": job.chunk_level,
                                "characters": len(chapter.text or ""),
                            }
                        )
                    elif chunks_for_chapter:
                        job.add_log(
                            "No audio generated for supplied chunks; chapter text also empty.",
                            level="warning",
                        )

            chapter_end_time = current_time

            if chapter_audio_path is not None:
                job.result.artifacts[f"chapter_{idx:02d}"] = chapter_audio_path
                chapter_paths.append(chapter_audio_path)

            if segments_emitted == 0:
                job.add_log(
                    f"No audio segments were generated for chapter {idx}.",
                    level="warning",
                )
            else:
                job.add_log(f"Finished chapter {idx} with {segments_emitted} segments.")

            if (
                audio_sink
                and job.merge_chapters_at_end
                and idx < total_chapters
                and job.silence_between_chapters > 0
            ):
                append_silence(
                    job.silence_between_chapters,
                    include_in_chapter=False,
                    chapter_sink=None,
                )
                chapter_end_time = current_time

            chapter_markers.append(
                {
                    "index": idx,
                    "title": chapter.title,
                    "start": chapter_start_time,
                    "end": chapter_end_time,
                    "voice": chapter_voice_spec,
                }
            )

        if not audio_path and chapter_paths:
            job.result.audio_path = chapter_paths[0]

        metadata_payload = {
            "metadata": dict(job.metadata_tags or {}),
            "chapters": chapter_markers,
            "chunks": chunk_markers,
            "chunk_level": job.chunk_level,
            "speaker_mode": job.speaker_mode,
            "speakers": dict(getattr(job, "speakers", {}) or {}),
            "generate_epub3": job.generate_epub3,
        }

        if metadata_dir:
            metadata_dir.mkdir(parents=True, exist_ok=True)
            metadata_file = metadata_dir / "metadata.json"
            metadata_file.write_text(json.dumps(metadata_payload, indent=2), encoding="utf-8")
            job.result.artifacts["metadata"] = metadata_file

        if job.generate_epub3:
            audio_asset = job.result.audio_path
            if not audio_asset and chapter_paths:
                audio_asset = chapter_paths[0]

            if audio_asset:
                try:
                    epub_root = project_root if job.save_as_project else base_output_dir
                    epub_output_path = _build_output_path(epub_root, job.original_filename, "epub")
                    job.add_log("Generating EPUB 3 package with synchronized narration…")
                    epub_path = build_epub3_package(
                        output_path=epub_output_path,
                        book_id=job.id,
                        extraction=extraction,
                        metadata_tags=metadata_payload.get("metadata") or {},
                        chapter_markers=chapter_markers,
                        chunk_markers=chunk_markers,
                        chunks=job.chunks,
                        audio_path=audio_asset,
                        speaker_mode=job.speaker_mode,
                        cover_image_path=job.cover_image_path,
                        cover_image_mime=job.cover_image_mime,
                    )
                    job.result.epub_path = epub_path
                    job.result.artifacts["epub3"] = epub_path
                    job.add_log(f"EPUB 3 package created at {epub_path}")
                except Exception as exc:
                    job.add_log(f"Failed to generate EPUB 3 package: {exc}", level="error")
            else:
                job.add_log("Skipped EPUB 3 generation: audio output unavailable.", level="warning")

        if job.save_as_project:
            job.result.artifacts["project_root"] = project_root

        if job.status != JobStatus.CANCELLED:
            job.progress = 1.0

        audio_output_path = job.result.audio_path

    except _JobCancelled:
        job.status = JobStatus.CANCELLED
        job.add_log("Job cancelled", level="warning")
    except Exception as exc:  # pragma: no cover - defensive guard
        job.error = str(exc)
        job.status = JobStatus.FAILED
        exc_type = exc.__class__.__name__
        job.add_log(f"Job failed ({exc_type}): {exc}", level="error")

        chapter_count: Any
        if extraction is not None and hasattr(extraction, "chapters"):
            try:
                chapter_count = len(getattr(extraction, "chapters", []) or [])
            except Exception:  # pragma: no cover - defensive fallback
                chapter_count = "unavailable"
        else:
            chapter_count = "unavailable"

        try:
            chunk_group_count = len(chunk_groups)
            chunk_total = sum(len(items) for items in chunk_groups.values())
        except Exception:  # pragma: no cover - defensive fallback
            chunk_group_count = "unavailable"
            chunk_total = "unavailable"

        job.add_log(
            "Context => chunk_level=%s, chapters=%s, chunk_groups=%s, chunks=%s"
            % (job.chunk_level, chapter_count, chunk_group_count, chunk_total),
            level="debug",
        )

        first_nonempty_group = next((items for items in chunk_groups.values() if items), None)
        if first_nonempty_group:
            first_chunk = dict(first_nonempty_group[0])
            sample_text = str(first_chunk.get("text") or "")[:160].replace("\n", " ")
            job.add_log(
                "First chunk sample => id=%s, speaker=%s, chars=%s, preview=%s"
                % (
                    first_chunk.get("id") or first_chunk.get("chunk_index"),
                    first_chunk.get("speaker_id", "narrator"),
                    len(str(first_chunk.get("text") or "")),
                    sample_text,
                ),
                level="debug",
            )

        tb_lines = traceback.format_exception(exc.__class__, exc, exc.__traceback__)
        for line in tb_lines[:20]:
            trimmed = line.rstrip()
            if trimmed:
                for snippet in trimmed.splitlines():
                    job.add_log(f"TRACE: {snippet}", level="debug")
    finally:
        sink_stack.close()
        if subtitle_writer:
            subtitle_writer.close()

        if (
            audio_output_path
            and job.output_format.lower() == "m4b"
            and not job.cancel_requested
            and job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}
        ):
            try:
                _embed_m4b_metadata(audio_output_path, metadata_payload, job)
            except Exception as exc:  # pragma: no cover - ensure failure propagates
                job.add_log(
                    f"Failed to embed metadata into m4b output: {exc}",
                    level="error",
                )
                raise RuntimeError(
                    f"Failed to embed metadata into m4b output: {exc}"
                ) from exc


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
    from platformdirs import user_desktop_dir  # type: ignore[import-not-found]

    default_output = Path(str(get_user_cache_path("outputs")))
    if job.save_mode == "Save to Desktop":
        directory = Path(user_desktop_dir())
    elif job.save_mode == "Save next to input file":
        directory = job.stored_path.parent
    elif job.save_mode == "Choose output folder" and job.output_folder:
        directory = Path(job.output_folder)
    elif job.save_mode == "Use default save location":
        directory = Path(get_user_output_path())
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
    ffmpeg_cache_root = get_internal_cache_path("ffmpeg")
    platform_cache = os.path.join(ffmpeg_cache_root, sys.platform)
    os.makedirs(platform_cache, exist_ok=True)
    try:
        import static_ffmpeg.run as static_ffmpeg_run  # type: ignore

        static_ffmpeg_run.LOCK_FILE = os.path.join(ffmpeg_cache_root, "lock.file")
    except Exception:
        pass

    static_ffmpeg.add_paths(weak=True, download_dir=platform_cache)
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
        process.stdin.write(data.tobytes())  # type: ignore[arg-type]

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
        base.extend(_metadata_to_ffmpeg_args(metadata))
    base.append(str(path))
    return base


def _resolve_voice(pipeline, voice_spec: str, use_gpu: bool):
    if "*" in voice_spec:
        return get_new_voice(pipeline, voice_spec, use_gpu)
    return voice_spec


def _to_float32(audio_segment) -> np.ndarray:
    if audio_segment is None:
        return np.zeros(0, dtype="float32")

    tensor = audio_segment
    if hasattr(tensor, "detach"):
        tensor = tensor.detach()
    if hasattr(tensor, "cpu"):
        try:
            tensor = tensor.cpu()
        except Exception:
            pass
    if hasattr(tensor, "numpy"):
        return np.asarray(tensor.numpy(), dtype="float32").reshape(-1)
    return np.asarray(tensor, dtype="float32").reshape(-1)


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
