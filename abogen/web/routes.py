from __future__ import annotations

import base64
import io
import json
import math
import mimetypes
import os
import posixpath
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple, cast
from xml.etree import ElementTree as ET

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask.typing import ResponseReturnValue

import numpy as np
import soundfile as sf

from werkzeug.utils import secure_filename

from abogen.chunking import ChunkLevel, build_chunks_for_chapters
from abogen.constants import (
    LANGUAGE_DESCRIPTIONS,
    SAMPLE_VOICE_TEXTS,
    SUBTITLE_FORMATS,
    SUPPORTED_LANGUAGES_FOR_SUBTITLE_GENERATION,
    SUPPORTED_SOUND_FORMATS,
    VOICES_INTERNAL,
)
from abogen.kokoro_text_normalization import normalize_for_pipeline, normalize_roman_numeral_titles
from abogen.normalization_settings import (
    DEFAULT_LLM_PROMPT,
    NORMALIZATION_SAMPLE_TEXTS,
    apply_overrides as apply_normalization_overrides,
    build_apostrophe_config,
    build_llm_configuration,
    clear_cached_settings,
    environment_llm_defaults,
    get_runtime_settings,
)
from abogen.llm_client import LLMClientError, LLMConfiguration, generate_completion, list_models
from abogen.utils import (
    calculate_text_length,
    get_user_output_path,
    load_config,
    load_numpy_kpipeline,
    save_config,
)
from abogen.entity_analysis import (
    extract_entities,
    merge_override,
    normalize_token as normalize_entity_token,
    search_tokens as search_entity_tokens,
)
from abogen.pronunciation_store import (
    delete_override as delete_pronunciation_override,
    load_overrides as load_pronunciation_overrides,
    all_overrides as all_pronunciation_overrides,
    save_override as save_pronunciation_override,
    search_overrides as search_pronunciation_overrides,
)
from abogen.voice_profiles import (
    delete_profile,
    duplicate_profile,
    export_profiles_payload,
    import_profiles_data,
    load_profiles,
    normalize_voice_entries,
    remove_profile,
    save_profile,
    save_profiles,
    serialize_profiles,
)

from abogen.voice_formulas import get_new_voice, parse_formula_terms
from abogen.speaker_analysis import analyze_speakers
from abogen.speaker_configs import (
    delete_config,
    get_config,
    list_configs,
    load_configs,
    save_configs,
    upsert_config,
    slugify_label,
)
from abogen.text_extractor import extract_from_path
from .conversion_runner import SPLIT_PATTERN, SAMPLE_RATE, _select_device, _to_float32
from .service import ConversionService, Job, JobStatus, PendingJob

web_bp = Blueprint("web", __name__)
api_bp = Blueprint("api", __name__)


_preview_pipeline_lock = threading.RLock()
_preview_pipelines: Dict[Tuple[str, str], Any] = {}


_CHUNK_LEVEL_OPTIONS = [
    {"value": "paragraph", "label": "Paragraphs"},
    {"value": "sentence", "label": "Sentences"},
]

_CHUNK_LEVEL_VALUES = {option["value"] for option in _CHUNK_LEVEL_OPTIONS}


_DEFAULT_ANALYSIS_THRESHOLD = 3


_WIZARD_STEP_ORDER = ["book", "chapters", "entities"]
_WIZARD_STEP_META = {
    "book": {
        "index": 1,
        "title": "Book parameters",
        "hint": "Choose your source file or paste text, then set the defaults used for chapter analysis and speaker casting.",
    },
    "chapters": {
        "index": 2,
        "title": "Select chapters",
        "hint": "Choose which chapters to convert. We'll analyse entities automatically when you continue.",
    },
    "entities": {
        "index": 3,
        "title": "Review entities",
        "hint": "Assign pronunciations, voices, and manual overrides before queueing the conversion.",
    },
}


def _coerce_path(value: Any) -> Optional[Path]:
    if isinstance(value, Path):
        return value
    if isinstance(value, str):
        candidate = Path(value)
        return candidate
    return None


def _normalize_epub_path(base_dir: str, href: str) -> str:
    if not href:
        return ""
    sanitized = href.split("#", 1)[0].split("?", 1)[0].strip()
    sanitized = sanitized.replace("\\", "/")
    if not sanitized:
        return ""
    if sanitized.startswith("/"):
        sanitized = sanitized[1:]
        base_dir = ""
    normalized_base = base_dir.strip("/")
    sanitized_lower = sanitized.lower()
    if normalized_base:
        base_lower = normalized_base.lower()
        prefix = base_lower + "/"
        if sanitized_lower.startswith(prefix):
            remainder = sanitized[len(prefix):]
            if remainder.lower().startswith(prefix):
                sanitized = remainder
                sanitized_lower = sanitized.lower()
            base_dir = ""
        elif sanitized_lower == base_lower:
            base_dir = ""
    base = base_dir.strip("/")
    combined = posixpath.join(base, sanitized) if base else sanitized
    normalized = posixpath.normpath(combined)
    if normalized in {"", "."}:
        return ""
    normalized = normalized.replace("\\", "/")
    segments = [segment for segment in normalized.split("/") if segment and segment != "."]
    if not segments:
        return ""
    deduped: List[str] = []
    last_lower: Optional[str] = None
    for segment in segments:
        segment_lower = segment.lower()
        if last_lower == segment_lower:
            continue
        deduped.append(segment)
        last_lower = segment_lower
    normalized = "/".join(deduped)
    if normalized.startswith("../") or normalized == "..":
        return ""
    return normalized


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "windows-1252"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", "ignore")


def _coerce_positive_time(value: Any) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric


def _load_job_metadata(job: Job) -> Dict[str, Any]:
    result = getattr(job, "result", None)
    artifacts = getattr(result, "artifacts", None)
    if not isinstance(artifacts, Mapping):
        return {}
    metadata_ref = artifacts.get("metadata")
    if isinstance(metadata_ref, Path):
        metadata_path = metadata_ref
    elif isinstance(metadata_ref, str):
        metadata_path = Path(metadata_ref)
    else:
        return {}
    if not metadata_path.exists():
        return {}
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _resolve_book_title(job: Job, *metadata_sources: Mapping[str, Any]) -> str:
    for source in metadata_sources:
        if not isinstance(source, Mapping):
            continue
        for key in ("title", "book_title", "name", "album", "album_title"):
            value = source.get(key)
            if isinstance(value, str):
                candidate = value.strip()
                if candidate:
                    return candidate
    filename = job.original_filename or ""
    stem = Path(filename).stem if filename else ""
    return stem or filename


class _NavMapParser(HTMLParser):
    def __init__(self, base_dir: str) -> None:
        super().__init__()
        self._base_dir = base_dir
        self._in_nav = False
        self._nav_depth = 0
        self._current_href: Optional[str] = None
        self._buffer: List[str] = []
        self.links: Dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag_lower = tag.lower()
        if tag_lower == "nav":
            attributes = dict(attrs)
            nav_type = (attributes.get("epub:type") or attributes.get("type") or "").strip().lower()
            nav_role = (attributes.get("role") or "").strip().lower()
            type_tokens = {token.strip() for token in nav_type.split() if token}
            role_tokens = {token.strip() for token in nav_role.split() if token}
            if "toc" in type_tokens or "doc-toc" in role_tokens:
                self._in_nav = True
                self._nav_depth = 1
                return
            if self._in_nav:
                self._nav_depth += 1
            return
        if not self._in_nav:
            return
        if tag_lower == "a":
            attributes = dict(attrs)
            href = attributes.get("href") or ""
            normalized = _normalize_epub_path(self._base_dir, href)
            if normalized:
                self._current_href = normalized
                self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "nav" and self._in_nav:
            self._nav_depth -= 1
            if self._nav_depth <= 0:
                self._in_nav = False
            return
        if not self._in_nav:
            return
        if tag_lower == "a" and self._current_href:
            text = "".join(self._buffer).strip()
            if text:
                self.links.setdefault(self._current_href, text)
            self._current_href = None
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._in_nav and self._current_href and data:
            self._buffer.append(data)


def _parse_nav_document(payload: bytes, base_dir: str) -> Dict[str, str]:
    parser = _NavMapParser(base_dir)
    parser.feed(_decode_text(payload))
    parser.close()
    return parser.links


def _parse_ncx_document(payload: bytes, base_dir: str) -> Dict[str, str]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return {}
    nav_map: Dict[str, str] = {}
    for nav_point in root.findall(".//{*}navPoint"):
        content = nav_point.find(".//{*}content")
        if content is None:
            continue
        src = content.attrib.get("src", "")
        normalized = _normalize_epub_path(base_dir, src)
        if not normalized:
            continue
        label_el = nav_point.find(".//{*}text")
        label = (label_el.text or "").strip() if label_el is not None and label_el.text else ""
        if not label:
            label = posixpath.basename(normalized) or f"Section {len(nav_map) + 1}"
        nav_map.setdefault(normalized, label)
    return nav_map


def _extract_epub_chapters(epub_path: Path) -> List[Dict[str, str]]:
    chapters: List[Dict[str, str]] = []
    if not epub_path or not epub_path.exists():
        return chapters
    try:
        with zipfile.ZipFile(epub_path, "r") as archive:
            container_bytes = archive.read("META-INF/container.xml")
            container_root = ET.fromstring(container_bytes)
            rootfile = container_root.find(".//{*}rootfile")
            if rootfile is None:
                return chapters
            opf_path = (rootfile.attrib.get("full-path") or "").strip()
            if not opf_path:
                return chapters
            opf_dir = posixpath.dirname(opf_path)
            opf_bytes = archive.read(opf_path)
            opf_root = ET.fromstring(opf_bytes)

            manifest: Dict[str, Dict[str, str]] = {}
            for item in opf_root.findall(".//{*}manifest/{*}item"):
                item_id = item.attrib.get("id")
                href = item.attrib.get("href")
                if not item_id or not href:
                    continue
                manifest[item_id] = {
                    "href": _normalize_epub_path(opf_dir, href),
                    "properties": item.attrib.get("properties", ""),
                    "media_type": item.attrib.get("media-type", ""),
                }

            spine_hrefs: List[str] = []
            nav_id: Optional[str] = None
            spine = opf_root.find(".//{*}spine")
            if spine is not None:
                nav_id = spine.attrib.get("toc")
                for itemref in spine.findall(".//{*}itemref"):
                    idref = itemref.attrib.get("idref")
                    if not idref:
                        continue
                    entry = manifest.get(idref)
                    if not entry:
                        continue
                    href = entry["href"]
                    if href and href not in spine_hrefs:
                        spine_hrefs.append(href)

            nav_href: Optional[str] = None
            for entry in manifest.values():
                properties = entry.get("properties") or ""
                if "nav" in {token.strip() for token in properties.split() if token}:
                    nav_href = entry["href"]
                    break
            if not nav_href and nav_id:
                toc_entry = manifest.get(nav_id)
                if toc_entry:
                    nav_href = toc_entry["href"]

            nav_titles: Dict[str, str] = {}
            if nav_href:
                nav_base = posixpath.dirname(nav_href)
                try:
                    nav_bytes = archive.read(nav_href)
                except KeyError:
                    nav_bytes = None
                if nav_bytes is not None:
                    if nav_href.lower().endswith(".ncx"):
                        nav_titles = _parse_ncx_document(nav_bytes, nav_base)
                    else:
                        nav_titles = _parse_nav_document(nav_bytes, nav_base)

            if not nav_titles and nav_id and nav_id in manifest:
                toc_entry = manifest[nav_id]
                nav_base = posixpath.dirname(toc_entry["href"])
                try:
                    nav_bytes = archive.read(toc_entry["href"])
                except KeyError:
                    nav_bytes = None
                if nav_bytes is not None:
                    nav_titles = _parse_ncx_document(nav_bytes, nav_base)

            for index, href in enumerate(spine_hrefs, start=1):
                normalized = href
                if not normalized:
                    continue
                title = (
                    nav_titles.get(normalized)
                    or nav_titles.get(normalized.split("#", 1)[0])
                    or posixpath.basename(normalized)
                    or f"Chapter {index}"
                )
                chapters.append({"href": normalized, "title": title})

            if not chapters and nav_titles:
                for index, (href, title) in enumerate(nav_titles.items(), start=1):
                    normalized = href
                    if not normalized:
                        continue
                    label = title or posixpath.basename(normalized) or f"Chapter {index}"
                    chapters.append({"href": normalized, "title": label})

            return chapters
    except (FileNotFoundError, zipfile.BadZipFile, KeyError, ET.ParseError, UnicodeDecodeError):
        return []
    return chapters


def _read_epub_bytes(epub_path: Path, raw_href: str) -> bytes:
    normalized = _normalize_epub_path("", raw_href)
    if not normalized:
        raise ValueError("Invalid resource path")
    with zipfile.ZipFile(epub_path, "r") as archive:
        return archive.read(normalized)


def _iter_job_result_paths(job: Job) -> List[Path]:
    result = getattr(job, "result", None)
    if result is None:
        return []
    resolved_seen: Set[Path] = set()
    collected: List[Path] = []

    def _remember(candidate: Optional[Path]) -> None:
        if not candidate:
            return
        try:
            resolved = candidate.resolve()
        except OSError:
            return
        if resolved in resolved_seen:
            return
        resolved_seen.add(resolved)
        collected.append(candidate)

    artifacts = getattr(result, "artifacts", None)
    if isinstance(artifacts, Mapping):
        for value in artifacts.values():
            candidate = _coerce_path(value)
            if candidate and candidate.exists() and candidate.is_file():
                _remember(candidate)

    for attr in ("audio_path", "epub_path"):
        candidate = _coerce_path(getattr(result, attr, None))
        if candidate and candidate.exists() and candidate.is_file():
            _remember(candidate)

    return collected


def _iter_job_artifact_dirs(job: Job) -> List[Path]:
    result = getattr(job, "result", None)
    if result is None:
        return []
    artifacts = getattr(result, "artifacts", None)
    directories: List[Path] = []
    if isinstance(artifacts, Mapping):
        for value in artifacts.values():
            candidate = _coerce_path(value)
            if candidate and candidate.exists() and candidate.is_dir():
                directories.append(candidate)
    return directories


def _normalize_suffixes(suffixes: Iterable[str]) -> List[str]:
    normalized: List[str] = []
    for suffix in suffixes:
        if not suffix:
            continue
        cleaned = suffix.lower().strip()
        if not cleaned:
            continue
        if not cleaned.startswith("."):
            cleaned = f".{cleaned.lstrip('.')}"
        normalized.append(cleaned)
    return normalized


def _find_job_file(job: Job, suffixes: Iterable[str]) -> Optional[Path]:
    ordered_suffixes = _normalize_suffixes(suffixes)
    if not ordered_suffixes:
        return None
    files = _iter_job_result_paths(job)
    for suffix in ordered_suffixes:
        for candidate in files:
            if candidate.suffix.lower() == suffix:
                return candidate
    directories = _iter_job_artifact_dirs(job)
    for suffix in ordered_suffixes:
        pattern = f"*{suffix}"
        for directory in directories:
            try:
                match = next((path for path in directory.rglob(pattern) if path.is_file()), None)
            except OSError:
                match = None
            if match:
                return match
    return None


def _locate_job_epub(job: Job) -> Optional[Path]:
    path = _find_job_file(job, [".epub"])
    if path:
        return path
    return None


def _locate_job_m4b(job: Job) -> Optional[Path]:
    return _find_job_file(job, [".m4b"])


def _locate_job_audio(job: Job, preferred_suffixes: Optional[Iterable[str]] = None) -> Optional[Path]:
    suffix_order: List[str] = []
    if preferred_suffixes:
        suffix_order.extend(preferred_suffixes)
    suffix_order.extend([".m4b", ".mp3", ".flac", ".opus", ".ogg", ".m4a", ".wav"])
    path = _find_job_file(job, suffix_order)
    if path:
        return path
    files = _iter_job_result_paths(job)
    return files[0] if files else None


def _job_download_flags(job: Job) -> Dict[str, bool]:
    if job.status != JobStatus.COMPLETED:
        return {"audio": False, "m4b": False, "epub3": False}
    return {
        "audio": _locate_job_audio(job) is not None,
        "m4b": _locate_job_m4b(job) is not None,
        "epub3": _locate_job_epub(job) is not None,
    }
def _build_narrator_roster(
    voice: str,
    voice_profile: Optional[str],
    existing: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    roster: Dict[str, Any] = {
        "narrator": {
            "id": "narrator",
            "label": "Narrator",
            "voice": voice,
        }
    }
    if voice_profile:
        roster["narrator"]["voice_profile"] = voice_profile
    existing_entry: Optional[Mapping[str, Any]] = None
    if existing is not None:
        existing_entry = existing.get("narrator") if isinstance(existing, Mapping) else None
    if isinstance(existing_entry, Mapping):
        roster_entry = roster["narrator"]
        for key in ("label", "voice", "voice_profile", "voice_formula", "pronunciation"):
            value = existing_entry.get(key)
            if value is not None and value != "":
                roster_entry[key] = value
    return roster


def _build_speaker_roster(
    analysis: Dict[str, Any],
    base_voice: str,
    voice_profile: Optional[str],
    existing: Optional[Mapping[str, Any]] = None,
    order: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    roster = _build_narrator_roster(base_voice, voice_profile, existing)
    existing_map: Dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    speakers = analysis.get("speakers", {}) if isinstance(analysis, dict) else {}
    ordered_ids: Iterable[str]
    if order is not None:
        ordered_ids = [sid for sid in order if sid in speakers]
    else:
        ordered_ids = speakers.keys()

    for speaker_id in ordered_ids:
        payload = speakers.get(speaker_id, {})
        if speaker_id == "narrator":
            continue
        if isinstance(payload, Mapping) and payload.get("suppressed"):
            continue
        previous = existing_map.get(speaker_id)
        roster[speaker_id] = {
            "id": speaker_id,
            "label": payload.get("label") or speaker_id.replace("_", " ").title(),
            "analysis_confidence": payload.get("confidence"),
            "analysis_count": payload.get("count"),
            "gender": payload.get("gender", "unknown"),
        }
        detected_gender = payload.get("detected_gender")
        if detected_gender:
            roster[speaker_id]["detected_gender"] = detected_gender
        samples = payload.get("sample_quotes")
        if isinstance(samples, list):
            roster[speaker_id]["sample_quotes"] = samples
        if isinstance(previous, Mapping):
            for key in ("voice", "voice_profile", "voice_formula", "resolved_voice", "pronunciation"):
                value = previous.get(key)
                if value is not None and value != "":
                    roster[speaker_id][key] = value
            if "sample_quotes" not in roster[speaker_id]:
                prev_samples = previous.get("sample_quotes")
                if isinstance(prev_samples, list):
                    roster[speaker_id]["sample_quotes"] = prev_samples
            if "detected_gender" not in roster[speaker_id]:
                prev_detected = previous.get("detected_gender")
                if isinstance(prev_detected, str) and prev_detected:
                    roster[speaker_id]["detected_gender"] = prev_detected
    return roster


def _match_configured_speaker(
    config_speakers: Mapping[str, Any],
    roster_id: str,
    roster_label: str,
) -> Optional[Mapping[str, Any]]:
    if not config_speakers:
        return None
    entry = config_speakers.get(roster_id)
    if entry:
        return cast(Mapping[str, Any], entry)
    slug = slugify_label(roster_label)
    if slug != roster_id and slug in config_speakers:
        return cast(Mapping[str, Any], config_speakers[slug])
    lower_label = roster_label.strip().lower()
    for record in config_speakers.values():
        if not isinstance(record, Mapping):
            continue
        if str(record.get("label", "")).strip().lower() == lower_label:
            return record
    return None


def _apply_speaker_config_to_roster(
    roster: Mapping[str, Any],
    config: Optional[Mapping[str, Any]],
    *,
    persist_changes: bool = False,
    fallback_languages: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, Any], List[str], Optional[Dict[str, Any]]]:
    if not isinstance(roster, Mapping):
        effective_languages = [code for code in (fallback_languages or []) if isinstance(code, str) and code]
        return {}, effective_languages, None
    updated_roster: Dict[str, Any] = {key: dict(value) for key, value in roster.items() if isinstance(value, Mapping)}
    if not config:
        effective_languages = [code for code in (fallback_languages or []) if isinstance(code, str) and code]
        return updated_roster, effective_languages, None

    speakers_map = config.get("speakers")
    if not isinstance(speakers_map, Mapping):
        effective_languages = [code for code in (fallback_languages or []) if isinstance(code, str) and code]
        return updated_roster, effective_languages, None

    config_languages = config.get("languages")
    if isinstance(config_languages, list):
        allowed_languages = [code for code in config_languages if isinstance(code, str) and code]
    else:
        allowed_languages = []
    if not allowed_languages and fallback_languages:
        allowed_languages = [code for code in fallback_languages if isinstance(code, str) and code]

    default_voice = config.get("default_voice") if isinstance(config.get("default_voice"), str) else ""
    used_voices = {entry.get("resolved_voice") or entry.get("voice") for entry in updated_roster.values()} - {None}
    narrator_voice = ""
    narrator_entry = updated_roster.get("narrator") if isinstance(updated_roster, Mapping) else None
    if isinstance(narrator_entry, Mapping):
        narrator_voice = str(
            narrator_entry.get("resolved_voice")
            or narrator_entry.get("default_voice")
            or ""
        ).strip()
        if narrator_voice:
            used_voices.add(narrator_voice)

    config_changed = False
    new_config_payload: Dict[str, Any] = {
        "language": config.get("language", "a"),
        "languages": allowed_languages,
        "default_voice": default_voice,
        "speakers": dict(speakers_map),
        "version": config.get("version", 1),
        "notes": config.get("notes", ""),
    }

    speakers_payload = new_config_payload["speakers"]

    for speaker_id, roster_entry in updated_roster.items():
        if speaker_id == "narrator":
            continue
        label = str(roster_entry.get("label") or speaker_id)
        config_entry = _match_configured_speaker(speakers_map, speaker_id, label)
        if config_entry is None:
            continue
        voice_id = str(config_entry.get("voice") or "").strip()
        voice_profile = str(config_entry.get("voice_profile") or "").strip()
        voice_formula = str(config_entry.get("voice_formula") or "").strip()
        resolved_voice = str(config_entry.get("resolved_voice") or "").strip()
        languages = config_entry.get("languages") if isinstance(config_entry.get("languages"), list) else []
        chosen_voice = resolved_voice or voice_formula or voice_id or roster_entry.get("voice")
        usable_languages = languages or allowed_languages

        if chosen_voice:
            roster_entry["resolved_voice"] = chosen_voice
            roster_entry["voice"] = chosen_voice if not voice_profile and not voice_formula else roster_entry.get("voice", chosen_voice)
        if voice_profile:
            roster_entry["voice_profile"] = voice_profile
        if voice_formula:
            roster_entry["voice_formula"] = voice_formula
            roster_entry["resolved_voice"] = voice_formula
        if not voice_formula and not voice_profile and resolved_voice:
            roster_entry["resolved_voice"] = resolved_voice
        roster_entry["config_languages"] = usable_languages or []

        if chosen_voice:
            used_voices.add(chosen_voice)

        # persist updates back to config payload if required
        if persist_changes:
            slug = config_entry.get("id") or slugify_label(label)
            speakers_payload[slug] = {
                "id": slug,
                "label": label,
                "gender": config_entry.get("gender", "unknown"),
                "voice": voice_id,
                "voice_profile": voice_profile,
                "voice_formula": voice_formula,
                "resolved_voice": roster_entry.get("resolved_voice", resolved_voice or voice_id),
                "languages": usable_languages,
            }

    new_config = new_config_payload if (persist_changes and config_changed) else None
    return updated_roster, allowed_languages, new_config


def _filter_voice_catalog(
    catalog: Iterable[Mapping[str, Any]],
    *,
    gender: str,
    allowed_languages: Optional[Iterable[str]] = None,
) -> List[str]:
    allowed_set = {code.lower() for code in (allowed_languages or []) if isinstance(code, str) and code}
    gender_normalized = (gender or "unknown").lower()
    gender_code = ""
    if gender_normalized == "male":
        gender_code = "m"
    elif gender_normalized == "female":
        gender_code = "f"

    matches: List[str] = []
    seen: set[str] = set()

    def _consider(entry: Mapping[str, Any]) -> None:
        voice_id = entry.get("id")
        if not isinstance(voice_id, str) or not voice_id:
            return
        if voice_id in seen:
            return
        seen.add(voice_id)
        matches.append(voice_id)

    primary: List[Mapping[str, Any]] = []
    fallback: List[Mapping[str, Any]] = []
    for entry in catalog:
        if not isinstance(entry, Mapping):
            continue
        voice_lang = str(entry.get("language", "")).lower()
        voice_gender_code = str(entry.get("gender_code", "")).lower()
        if allowed_set and voice_lang not in allowed_set:
            continue
        if gender_code and voice_gender_code != gender_code:
            fallback.append(entry)
            continue
        primary.append(entry)

    for entry in primary:
        _consider(entry)

    if not matches:
        for entry in fallback:
            _consider(entry)

    if not matches:
        for entry in catalog:
            if isinstance(entry, Mapping):
                _consider(entry)

    return matches


def _inject_recommended_voices(
    roster: Mapping[str, Any],
    *,
    fallback_languages: Optional[Iterable[str]] = None,
) -> None:
    voice_catalog = _build_voice_catalog()
    fallback_list = [code for code in (fallback_languages or []) if isinstance(code, str) and code]
    for speaker_id, payload in roster.items():
        if not isinstance(payload, dict):
            continue
        languages = payload.get("config_languages")
        if isinstance(languages, list) and languages:
            language_list = languages
        else:
            language_list = fallback_list
        gender = str(payload.get("gender", "unknown"))
        payload["recommended_voices"] = _filter_voice_catalog(
            voice_catalog,
            gender=gender,
            allowed_languages=language_list,
        )


def _extract_speaker_config_form(form: Mapping[str, Any]) -> Tuple[str, Dict[str, Any], List[str]]:
    getter = getattr(form, "getlist", None)

    def _get_list(name: str) -> List[str]:
        if callable(getter):
            values = cast(Iterable[Any], getter(name))
            return [str(value).strip() for value in values if value]
        raw_value = form.get(name)
        if isinstance(raw_value, str):
            return [item.strip() for item in raw_value.split(",") if item.strip()]
        return []

    name = (form.get("config_name") or "").strip()
    language = str(form.get("config_language") or "a").strip() or "a"
    allowed_languages = []
    default_voice = (form.get("config_default_voice") or "").strip()
    notes = (form.get("config_notes") or "").strip()
    version = _coerce_int(form.get("config_version"), 1, minimum=1, maximum=9999)

    speaker_rows = _get_list("speaker_rows")
    speakers: Dict[str, Dict[str, Any]] = {}
    for row_key in speaker_rows:
        prefix = f"speaker-{row_key}-"
        label = (form.get(prefix + "label") or "").strip()
        if not label:
            continue
        raw_gender = (form.get(prefix + "gender") or "unknown").strip().lower()
        gender = raw_gender if raw_gender in {"male", "female", "unknown"} else "unknown"
        voice = (form.get(prefix + "voice") or "").strip()
        voice_profile = (form.get(prefix + "profile") or "").strip()
        voice_formula = (form.get(prefix + "formula") or "").strip()
        speaker_id = (form.get(prefix + "id") or "").strip() or slugify_label(label)
        speakers[speaker_id] = {
            "id": speaker_id,
            "label": label,
            "gender": gender,
            "voice": voice,
            "voice_profile": voice_profile,
            "voice_formula": voice_formula,
            "resolved_voice": voice_formula or voice,
            "languages": [],
        }

    payload = {
        "language": language,
        "languages": allowed_languages,
        "default_voice": default_voice,
        "speakers": speakers,
        "notes": notes,
        "version": version,
    }

    errors: List[str] = []
    if not name:
        errors.append("Configuration name is required.")
    if not speakers:
        errors.append("Add at least one speaker to the configuration.")

    return name, payload, errors


def _prepare_speaker_metadata(
    *,
    chapters: List[Dict[str, Any]],
    chunks: List[Dict[str, Any]],
    analysis_chunks: Optional[List[Dict[str, Any]]] = None,
    voice: str,
    voice_profile: Optional[str],
    threshold: int,
    existing_roster: Optional[Mapping[str, Any]] = None,
    run_analysis: bool = True,
    speaker_config: Optional[Mapping[str, Any]] = None,
    apply_config: bool = False,
    persist_config: bool = False,
) -> tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any], List[str], Optional[Dict[str, Any]]]:
    chunk_list = [dict(chunk) for chunk in chunks]
    analysis_source = [dict(chunk) for chunk in (analysis_chunks or chunks)]
    threshold_value = max(1, int(threshold))
    analysis_enabled = run_analysis
    settings_state = _load_settings()
    global_random_languages = [
        code
        for code in settings_state.get("speaker_random_languages", [])
        if isinstance(code, str) and code
    ]

    if not analysis_enabled:
        for chunk in chunk_list:
            chunk["speaker_id"] = "narrator"
            chunk["speaker_label"] = "Narrator"
        analysis_payload = {
            "version": "1.0",
            "narrator": "narrator",
            "assignments": {str(chunk.get("id")): "narrator" for chunk in chunk_list},
            "speakers": {
                "narrator": {
                    "id": "narrator",
                    "label": "Narrator",
                    "count": len(chunk_list),
                    "confidence": "low",
                    "sample_quotes": [],
                    "suppressed": False,
                }
            },
            "suppressed": [],
            "stats": {
                "total_chunks": len(chunk_list),
                "explicit_chunks": 0,
                "active_speakers": 0,
                "unique_speakers": 1,
                "suppressed": 0,
            },
        }
        roster = _build_narrator_roster(voice, voice_profile, existing_roster)
        narrator_pron = roster["narrator"].get("pronunciation")
        if narrator_pron:
            analysis_payload["speakers"]["narrator"]["pronunciation"] = narrator_pron
        return chunk_list, roster, analysis_payload, [], None

    analysis_result = analyze_speakers(
        chapters,
        analysis_source,
        threshold=threshold_value,
        max_speakers=0,
    )
    analysis_payload = analysis_result.to_dict()
    speakers_payload = analysis_payload.get("speakers", {})
    ordered_ids = [
        sid
        for sid, meta in sorted(
            (
                (sid, meta)
                for sid, meta in speakers_payload.items()
                if sid != "narrator" and isinstance(meta, Mapping) and not meta.get("suppressed")
            ),
            key=lambda item: item[1].get("count", 0),
            reverse=True,
        )
    ]
    analysis_payload["ordered_speakers"] = ordered_ids
    assignments = analysis_payload.get("assignments", {})
    suppressed_ids = analysis_payload.get("suppressed", [])
    suppressed_details: List[Dict[str, Any]] = []
    speakers_payload = analysis_payload.get("speakers", {})
    if isinstance(suppressed_ids, Iterable):
        for suppressed_id in suppressed_ids:
            speaker_meta = speakers_payload.get(suppressed_id) if isinstance(speakers_payload, dict) else None
            if isinstance(speaker_meta, dict):
                suppressed_details.append(
                    {
                        "id": suppressed_id,
                        "label": speaker_meta.get("label")
                        or str(suppressed_id).replace("_", " ").title(),
                        "pronunciation": speaker_meta.get("pronunciation"),
                    }
                )
            else:
                suppressed_details.append(
                    {
                        "id": suppressed_id,
                        "label": str(suppressed_id).replace("_", " ").title(),
                        "pronunciation": None,
                    }
                )
    analysis_payload["suppressed_details"] = suppressed_details
    roster = _build_speaker_roster(
        analysis_payload,
        voice,
        voice_profile,
        existing=existing_roster,
        order=analysis_payload.get("ordered_speakers"),
    )
    applied_languages: List[str] = []
    updated_config: Optional[Dict[str, Any]] = None
    if apply_config and speaker_config:
        roster, applied_languages, updated_config = _apply_speaker_config_to_roster(
            roster,
            speaker_config,
            persist_changes=persist_config,
            fallback_languages=global_random_languages,
        )
        speakers_payload = analysis_payload.get("speakers")
        if isinstance(speakers_payload, dict):
            for roster_id, roster_payload in roster.items():
                speaker_meta = speakers_payload.get(roster_id)
                if isinstance(speaker_meta, dict):
                    for key in ("voice", "voice_profile", "voice_formula", "resolved_voice"):
                        value = roster_payload.get(key)
                        if value:
                            speaker_meta[key] = value
    effective_languages: List[str] = []
    if applied_languages:
        effective_languages = applied_languages
    elif isinstance(analysis_payload.get("config_languages"), list):
        effective_languages = [
            code for code in analysis_payload.get("config_languages", []) if isinstance(code, str) and code
        ]
    elif global_random_languages:
        effective_languages = list(global_random_languages)

    if effective_languages:
        analysis_payload["config_languages"] = effective_languages
    speakers_payload = analysis_payload.get("speakers")
    if isinstance(speakers_payload, dict):
        for roster_id, roster_payload in roster.items():
            if roster_id in speakers_payload and isinstance(roster_payload, dict):
                pronunciation_value = roster_payload.get("pronunciation")
                if pronunciation_value:
                    speakers_payload[roster_id]["pronunciation"] = pronunciation_value

    fallback_languages = effective_languages or []
    _inject_recommended_voices(roster, fallback_languages=fallback_languages)

    for chunk in chunk_list:
        chunk_id = str(chunk.get("id"))
        speaker_id = assignments.get(chunk_id, "narrator")
        chunk["speaker_id"] = speaker_id
        speaker_meta = roster.get(speaker_id)
        chunk["speaker_label"] = speaker_meta.get("label") if isinstance(speaker_meta, dict) else speaker_id

    return chunk_list, roster, analysis_payload, applied_languages, updated_config


def _collect_pronunciation_overrides(pending: PendingJob) -> List[Dict[str, Any]]:
    language = pending.language or "en"
    collected: Dict[str, Dict[str, Any]] = {}

    summary = pending.entity_summary or {}
    for group in ("people", "entities"):
        entries = summary.get(group)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            override_payload = entry.get("override")
            if not isinstance(override_payload, Mapping):
                continue
            token_value = str(entry.get("label") or override_payload.get("token") or "").strip()
            pronunciation_value = str(override_payload.get("pronunciation") or "").strip()
            if not token_value or not pronunciation_value:
                continue
            normalized = normalize_entity_token(entry.get("normalized") or token_value)
            if not normalized:
                continue
            collected[normalized] = {
                "token": token_value,
                "normalized": normalized,
                "pronunciation": pronunciation_value,
                "voice": str(override_payload.get("voice") or "").strip() or None,
                "notes": str(override_payload.get("notes") or "").strip() or None,
                "context": str(override_payload.get("context") or "").strip() or None,
                "source": f"{group}-override",
                "language": language,
            }

    if isinstance(pending.speakers, Mapping):
        for speaker_payload in pending.speakers.values():
            if not isinstance(speaker_payload, Mapping):
                continue
            token_value = str(speaker_payload.get("label") or "").strip()
            pronunciation_value = str(speaker_payload.get("pronunciation") or "").strip()
            if not token_value or not pronunciation_value:
                continue
            normalized = normalize_entity_token(token_value)
            if not normalized:
                continue
            collected[normalized] = {
                "token": token_value,
                "normalized": normalized,
                "pronunciation": pronunciation_value,
                "voice": str(
                    speaker_payload.get("resolved_voice")
                    or speaker_payload.get("voice")
                    or pending.voice
                ).strip()
                or None,
                "notes": None,
                "context": None,
                "source": "speaker",
                "language": language,
            }

    for manual_entry in pending.manual_overrides or []:
        if not isinstance(manual_entry, Mapping):
            continue
        token_value = str(manual_entry.get("token") or "").strip()
        pronunciation_value = str(manual_entry.get("pronunciation") or "").strip()
        if not token_value or not pronunciation_value:
            continue
        normalized = manual_entry.get("normalized") or normalize_entity_token(token_value)
        if not normalized:
            continue
        collected[normalized] = {
            "token": token_value,
            "normalized": normalized,
            "pronunciation": pronunciation_value,
            "voice": str(manual_entry.get("voice") or "").strip() or None,
            "notes": str(manual_entry.get("notes") or "").strip() or None,
            "context": str(manual_entry.get("context") or "").strip() or None,
            "source": str(manual_entry.get("source") or "manual"),
            "language": language,
        }

    return list(collected.values())


def _sync_pronunciation_overrides(pending: PendingJob) -> None:
    pending.pronunciation_overrides = _collect_pronunciation_overrides(pending)

    if not pending.pronunciation_overrides:
        return

    summary = pending.entity_summary or {}
    manual_map: Dict[str, Mapping[str, Any]] = {}
    for override in pending.manual_overrides or []:
        if not isinstance(override, Mapping):
            continue
        normalized = override.get("normalized") or normalize_entity_token(override.get("token") or "")
        pronunciation_value = str(override.get("pronunciation") or "").strip()
        if not normalized or not pronunciation_value:
            continue
        manual_map[normalized] = override
    for group in ("people", "entities"):
        entries = summary.get(group)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            normalized = normalize_entity_token(entry.get("normalized") or entry.get("label") or "")
            manual_override = manual_map.get(normalized)
            if manual_override:
                entry["override"] = {
                    "token": manual_override.get("token"),
                    "pronunciation": manual_override.get("pronunciation"),
                    "voice": manual_override.get("voice"),
                    "notes": manual_override.get("notes"),
                    "context": manual_override.get("context"),
                    "source": manual_override.get("source"),
                }


def _refresh_entity_summary(pending: PendingJob, chapters: Iterable[Mapping[str, Any]]) -> None:
    settings = _load_settings()
    if not bool(settings.get("enable_entity_recognition", True)):
        pending.entity_summary = {}
        pending.entity_cache_key = ""
        pending.pronunciation_overrides = pending.pronunciation_overrides or []
        return

    language = pending.language or "en"
    chapter_list: List[Mapping[str, Any]] = [chapter for chapter in chapters if isinstance(chapter, Mapping)]
    if not chapter_list:
        pending.entity_summary = {}
        pending.entity_cache_key = ""
        pending.pronunciation_overrides = pending.pronunciation_overrides or []
        return

    enabled_only = [chapter for chapter in chapter_list if chapter.get("enabled")]
    target_chapters = enabled_only or chapter_list
    result = extract_entities(target_chapters, language=language)
    summary = dict(result.summary)
    tokens: List[str] = []
    for group in ("people", "entities"):
        entries = summary.get(group)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            token_value = str(entry.get("normalized") or entry.get("label") or "").strip()
            if token_value:
                tokens.append(token_value)

    overrides_from_store = load_pronunciation_overrides(language=language, tokens=tokens)
    merged_summary = merge_override(summary, overrides_from_store)
    if result.errors:
        merged_summary["errors"] = list(result.errors)
    merged_summary["cache_key"] = result.cache_key
    pending.entity_summary = merged_summary
    pending.entity_cache_key = result.cache_key
    _sync_pronunciation_overrides(pending)


def _find_manual_override(pending: PendingJob, identifier: str) -> Optional[Dict[str, Any]]:
    for entry in pending.manual_overrides or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == identifier or entry.get("normalized") == identifier:
            return entry
    return None


def _upsert_manual_override(pending: PendingJob, payload: Mapping[str, Any]) -> Dict[str, Any]:
    token_value = str(payload.get("token") or "").strip()
    if not token_value:
        raise ValueError("Token is required")
    pronunciation_value = str(payload.get("pronunciation") or "").strip()
    voice_value = str(payload.get("voice") or "").strip()
    notes_value = str(payload.get("notes") or "").strip()
    context_value = str(payload.get("context") or "").strip()
    normalized = payload.get("normalized") or normalize_entity_token(token_value)
    if not normalized:
        raise ValueError("Token is required")

    existing = _find_manual_override(pending, payload.get("id", "")) or _find_manual_override(pending, normalized)
    timestamp = time.time()
    language = pending.language or "en"

    if existing:
        existing.update(
            {
                "token": token_value,
                "normalized": normalized,
                "pronunciation": pronunciation_value,
                "voice": voice_value,
                "notes": notes_value,
                "context": context_value,
                "updated_at": timestamp,
            }
        )
        manual_entry = existing
    else:
        manual_entry = {
            "id": payload.get("id") or uuid.uuid4().hex,
            "token": token_value,
            "normalized": normalized,
            "pronunciation": pronunciation_value,
            "voice": voice_value,
            "notes": notes_value,
            "context": context_value,
            "language": language,
            "source": payload.get("source") or "manual",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        if isinstance(pending.manual_overrides, list):
            pending.manual_overrides.append(manual_entry)
        else:
            pending.manual_overrides = [manual_entry]

    save_pronunciation_override(
        language=language,
        token=token_value,
        pronunciation=pronunciation_value or None,
        voice=voice_value or None,
        notes=notes_value or None,
        context=context_value or None,
    )

    _sync_pronunciation_overrides(pending)
    return dict(manual_entry)


def _delete_manual_override(pending: PendingJob, override_id: str) -> bool:
    if not override_id:
        return False
    entries = pending.manual_overrides or []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == override_id:
            token_value = entry.get("token") or ""
            language = pending.language or "en"
            delete_pronunciation_override(language=language, token=token_value)
            entries.pop(index)
            pending.manual_overrides = entries
            _sync_pronunciation_overrides(pending)
            return True
    return False


def _search_manual_override_candidates(pending: PendingJob, query: str, *, limit: int = 15) -> List[Dict[str, Any]]:
    normalized_query = (query or "").strip()
    summary_index = (pending.entity_summary or {}).get("index", {})
    matches = search_entity_tokens(summary_index, normalized_query, limit=limit)
    registry: Dict[str, Dict[str, Any]] = {}

    for entry in matches:
        normalized = normalize_entity_token(entry.get("normalized") or entry.get("token") or "")
        if not normalized:
            continue
        registry.setdefault(
            normalized,
            {
                "token": entry.get("token"),
                "normalized": normalized,
                "category": entry.get("category") or "entity",
                "count": entry.get("count", 0),
                "samples": entry.get("samples", []),
                "source": "entity",
            },
        )

    language = pending.language or "en"
    store_matches = search_pronunciation_overrides(language=language, query=normalized_query, limit=limit)
    for entry in store_matches:
        normalized = entry.get("normalized")
        if not normalized:
            continue
        registry.setdefault(
            normalized,
            {
                "token": entry.get("token"),
                "normalized": normalized,
                "category": "history",
                "count": entry.get("usage_count", 0),
                "samples": [entry.get("context")] if entry.get("context") else [],
                "source": "history",
                "pronunciation": entry.get("pronunciation"),
                "voice": entry.get("voice"),
            },
        )

    for entry in pending.manual_overrides or []:
        if not isinstance(entry, Mapping):
            continue
        normalized = entry.get("normalized")
        if not normalized:
            continue
        registry.setdefault(
            normalized,
            {
                "token": entry.get("token"),
                "normalized": normalized,
                "category": "manual",
                "count": 0,
                "samples": [entry.get("context")] if entry.get("context") else [],
                "source": "manual",
                "pronunciation": entry.get("pronunciation"),
                "voice": entry.get("voice"),
            },
        )

    ordered = sorted(registry.values(), key=lambda item: (-int(item.get("count") or 0), item.get("token") or ""))
    if limit:
        return ordered[:limit]
    return ordered


def _pending_entities_payload(pending: PendingJob) -> Dict[str, Any]:
    settings = _load_settings()
    recognition_enabled = bool(settings.get("enable_entity_recognition", True))
    return {
        "summary": pending.entity_summary or {},
        "manual_overrides": pending.manual_overrides or [],
        "pronunciation_overrides": pending.pronunciation_overrides or [],
        "cache_key": pending.entity_cache_key,
        "language": pending.language or "en",
        "recognition_enabled": recognition_enabled,
    }


def _apply_prepare_form(
    pending: PendingJob, form: Mapping[str, Any]
) -> tuple[
    ChunkLevel,
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    List[str],
    int,
    str,
    bool,
    bool,
]:
    raw_chunk_level = (form.get("chunk_level") or pending.chunk_level or "paragraph").strip().lower()
    if raw_chunk_level not in _CHUNK_LEVEL_VALUES:
        raw_chunk_level = pending.chunk_level if pending.chunk_level in _CHUNK_LEVEL_VALUES else "paragraph"
    pending.chunk_level = raw_chunk_level
    chunk_level_literal = cast(ChunkLevel, pending.chunk_level)

    pending.speaker_mode = "single"

    pending.generate_epub3 = _coerce_bool(form.get("generate_epub3"), False)

    threshold_default = getattr(pending, "speaker_analysis_threshold", _DEFAULT_ANALYSIS_THRESHOLD)
    raw_threshold = form.get("speaker_analysis_threshold")
    if raw_threshold is not None:
        pending.speaker_analysis_threshold = _coerce_int(
            raw_threshold,
            threshold_default,
            minimum=1,
            maximum=25,
        )
    else:
        pending.speaker_analysis_threshold = threshold_default

    if not pending.speakers:
        narrator: Dict[str, Any] = {
            "id": "narrator",
            "label": "Narrator",
            "voice": pending.voice,
        }
        if pending.voice_profile:
            narrator["voice_profile"] = pending.voice_profile
        pending.speakers = {"narrator": narrator}
    else:
        existing_narrator = pending.speakers.get("narrator")
        if isinstance(existing_narrator, dict):
            existing_narrator.setdefault("id", "narrator")
            existing_narrator["label"] = existing_narrator.get("label", "Narrator")
            existing_narrator["voice"] = pending.voice
            if pending.voice_profile:
                existing_narrator["voice_profile"] = pending.voice_profile
            pending.speakers["narrator"] = existing_narrator

    selected_config = (form.get("applied_speaker_config") or "").strip()
    apply_config_requested = str(form.get("apply_speaker_config", "")).strip() in {"1", "true", "on"}
    persist_config_requested = str(form.get("save_speaker_config", "")).strip() in {"1", "true", "on"}

    pending.applied_speaker_config = selected_config or None

    errors: List[str] = []

    if isinstance(pending.speakers, dict):
        for speaker_id, payload in list(pending.speakers.items()):
            if not isinstance(payload, dict):
                continue
            field_key = f"speaker-{speaker_id}-pronunciation"
            raw_value = form.get(field_key, "")
            pronunciation = raw_value.strip()
            if pronunciation:
                payload["pronunciation"] = pronunciation
            else:
                payload.pop("pronunciation", None)

            voice_value = (form.get(f"speaker-{speaker_id}-voice") or "").strip()
            formula_key = f"speaker-{speaker_id}-formula"
            formula_value = (form.get(formula_key) or "").strip()
            has_formula = False
            if formula_value:
                try:
                    _parse_voice_formula(formula_value)
                except ValueError as exc:
                    label = payload.get("label") or speaker_id.replace("_", " ").title()
                    errors.append(f"Invalid custom mix for {label}: {exc}")
                else:
                    payload["voice_formula"] = formula_value
                    payload["resolved_voice"] = formula_value
                    payload.pop("voice_profile", None)
                    has_formula = True
            else:
                payload.pop("voice_formula", None)

            if voice_value == "__custom_mix":
                voice_value = ""

            if voice_value:
                payload["voice"] = voice_value
                if not has_formula:
                    payload["resolved_voice"] = voice_value
            else:
                payload.pop("voice", None)
                if not has_formula:
                    payload.pop("resolved_voice", None)

            lang_key = f"speaker-{speaker_id}-languages"
            languages: List[str] = []
            getter = getattr(form, "getlist", None)
            if callable(getter):
                values = cast(Iterable[str], getter(lang_key))
                languages = [code.strip() for code in values if code]
            else:
                raw_langs = form.get(lang_key)
                if isinstance(raw_langs, str):
                    languages = [item.strip() for item in raw_langs.split(",") if item.strip()]
            payload["config_languages"] = languages

    profiles = serialize_profiles()
    raw_delay = form.get("chapter_intro_delay")
    if raw_delay is not None:
        raw_normalized = raw_delay.strip()
        if raw_normalized:
            try:
                pending.chapter_intro_delay = max(0.0, float(raw_normalized))
            except ValueError:
                errors.append("Enter a valid number for the chapter intro delay.")
        else:
            pending.chapter_intro_delay = 0.0

    overrides: List[Dict[str, Any]] = []
    selected_total = 0

    for index, chapter in enumerate(pending.chapters):
        enabled = form.get(f"chapter-{index}-enabled") == "on"
        title_input = (form.get(f"chapter-{index}-title") or "").strip()
        title = title_input or chapter.get("title") or f"Chapter {index + 1}"
        voice_selection = form.get(f"chapter-{index}-voice", "__default")
        formula_input = (form.get(f"chapter-{index}-formula") or "").strip()

        entry: Dict[str, Any] = {
            "id": chapter.get("id") or f"{index:04d}",
            "index": index,
            "order": index,
            "source_title": chapter.get("title") or title,
            "title": title,
            "text": chapter.get("text", ""),
            "enabled": enabled,
        }
        entry["characters"] = calculate_text_length(entry["text"])

        if enabled:
            if voice_selection.startswith("voice:"):
                entry["voice"] = voice_selection.split(":", 1)[1]
                entry["resolved_voice"] = entry["voice"]
            elif voice_selection.startswith("profile:"):
                profile_name = voice_selection.split(":", 1)[1]
                entry["voice_profile"] = profile_name
                profile_entry = profiles.get(profile_name) or {}
                formula_value = _formula_from_profile(profile_entry)
                if formula_value:
                    entry["voice_formula"] = formula_value
                    entry["resolved_voice"] = formula_value
                else:
                    errors.append(f"Profile '{profile_name}' has no configured voices.")
            elif voice_selection == "formula":
                if not formula_input:
                    errors.append(f"Provide a custom formula for chapter {index + 1}.")
                else:
                    try:
                        _parse_voice_formula(formula_input)
                    except ValueError as exc:
                        errors.append(str(exc))
                    else:
                        entry["voice_formula"] = formula_input
                        entry["resolved_voice"] = formula_input
            selected_total += entry["characters"]

        overrides.append(entry)
        pending.chapters[index] = dict(entry)

    enabled_overrides = [entry for entry in overrides if entry.get("enabled")]

    _sync_pronunciation_overrides(pending)

    return (
        chunk_level_literal,
        overrides,
        enabled_overrides,
        errors,
        selected_total,
        selected_config,
        apply_config_requested,
        persist_config_requested,
    )


def _apply_book_step_form(
    pending: PendingJob,
    form: Mapping[str, Any],
    *,
    settings: Mapping[str, Any],
    profiles: Mapping[str, Any],
) -> None:
    language_fallback = pending.language or settings.get("language", "en")
    raw_language = (form.get("language") or language_fallback or "en").strip()
    if raw_language:
        pending.language = raw_language

    subtitle_mode = (form.get("subtitle_mode") or pending.subtitle_mode or "Disabled").strip()
    if subtitle_mode:
        pending.subtitle_mode = subtitle_mode

    pending.generate_epub3 = _coerce_bool(form.get("generate_epub3"), bool(pending.generate_epub3))

    chunk_level_default = str(settings.get("chunk_level", "paragraph")).strip().lower()
    raw_chunk_level = (form.get("chunk_level") or pending.chunk_level or chunk_level_default).strip().lower()
    if raw_chunk_level not in _CHUNK_LEVEL_VALUES:
        raw_chunk_level = chunk_level_default if chunk_level_default in _CHUNK_LEVEL_VALUES else (pending.chunk_level or "paragraph")
    pending.chunk_level = raw_chunk_level

    threshold_default = pending.speaker_analysis_threshold or settings.get("speaker_analysis_threshold", _DEFAULT_ANALYSIS_THRESHOLD)
    raw_threshold = form.get("speaker_analysis_threshold")
    if raw_threshold is not None:
        pending.speaker_analysis_threshold = _coerce_int(
            raw_threshold,
            threshold_default,
            minimum=1,
            maximum=25,
        )

    raw_delay = form.get("chapter_intro_delay")
    if raw_delay is not None:
        try:
            pending.chapter_intro_delay = max(0.0, float(str(raw_delay).strip() or 0.0))
        except ValueError:
            pass

    speed_value = form.get("speed")
    if speed_value is not None:
        try:
            pending.speed = float(speed_value)
        except ValueError:
            pass

    profile_selection = (form.get("voice_profile") or pending.voice_profile or "__standard").strip()
    custom_formula_raw = (form.get("voice_formula") or "").strip()
    narrator_voice = (form.get("voice") or pending.voice or settings.get("default_voice") or "").strip()

    if profile_selection in {"__standard", "", None}:
        profile_name = ""
        custom_formula = ""
    elif profile_selection == "__formula":
        profile_name = ""
        custom_formula = custom_formula_raw
    else:
        profile_name = profile_selection
        custom_formula = ""

    profile_map = profiles if isinstance(profiles, dict) else dict(profiles)
    voice_choice, resolved_language, selected_profile = _resolve_voice_choice(
        pending.language,
        narrator_voice,
        profile_name,
        custom_formula,
        profile_map,
    )

    if resolved_language:
        pending.language = resolved_language

    if profile_selection == "__formula" and custom_formula_raw:
        pending.voice = custom_formula_raw
        pending.voice_profile = None
    elif profile_selection not in {"__standard", "", None, "__formula"}:
        pending.voice_profile = selected_profile or profile_selection
        pending.voice = voice_choice
    else:
        pending.voice_profile = None
        pending.voice = voice_choice or narrator_voice

    pending.applied_speaker_config = (form.get("speaker_config") or "").strip() or None

_SUPPLEMENT_TITLE_PATTERNS: List[tuple[re.Pattern[str], float]] = [
    (re.compile(r"\btitle\s+page\b"), 3.0),
    (re.compile(r"\bcopyright\b"), 2.4),
    (re.compile(r"\btable\s+of\s+contents\b"), 2.8),
    (re.compile(r"\bcontents\b"), 2.0),
    (re.compile(r"\backnowledg(e)?ments?\b"), 2.0),
    (re.compile(r"\bdedication\b"), 2.0),
    (re.compile(r"\babout\s+the\s+author(s)?\b"), 2.4),
    (re.compile(r"\balso\s+by\b"), 2.0),
    (re.compile(r"\bpraise\s+for\b"), 2.0),
    (re.compile(r"\bcolophon\b"), 2.2),
    (re.compile(r"\bpublication\s+data\b"), 2.2),
    (re.compile(r"\btranscriber'?s?\s+note\b"), 2.2),
    (re.compile(r"\bglossary\b"), 2.0),
    (re.compile(r"\bindex\b"), 2.0),
    (re.compile(r"\bbibliograph(y|ies)\b"), 2.0),
    (re.compile(r"\breferences\b"), 1.8),
    (re.compile(r"\bappendix\b"), 1.9),
]

_CONTENT_TITLE_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"\bchapter\b"),
    re.compile(r"\bbook\b"),
    re.compile(r"\bpart\b"),
    re.compile(r"\bsection\b"),
    re.compile(r"\bscene\b"),
    re.compile(r"\bprologue\b"),
    re.compile(r"\bepilogue\b"),
    re.compile(r"\bintroduction\b"),
    re.compile(r"\bstory\b"),
]

_SUPPLEMENT_TEXT_KEYWORDS: List[tuple[str, float]] = [
    ("copyright", 1.2),
    ("all rights reserved", 1.1),
    ("isbn", 0.9),
    ("library of congress", 1.0),
    ("table of contents", 1.0),
    ("dedicated to", 0.8),
    ("acknowledg", 0.8),
    ("printed in", 0.6),
    ("permission", 0.6),
    ("publisher", 0.5),
    ("praise for", 0.9),
    ("also by", 0.9),
    ("glossary", 0.8),
    ("index", 0.8),
    ("newsletter", 3.2),
    ("mailing list", 2.6),
    ("sign-up", 2.2),
]


def _supplement_score(title: str, text: str, index: int) -> float:
    normalized_title = (title or "").lower()
    score = 0.0

    for pattern, weight in _SUPPLEMENT_TITLE_PATTERNS:
        if pattern.search(normalized_title):
            score += weight

    for pattern in _CONTENT_TITLE_PATTERNS:
        if pattern.search(normalized_title):
            score -= 2.0

    stripped_text = (text or "").strip()
    length = len(stripped_text)
    if length <= 150:
        score += 0.9
    elif length <= 400:
        score += 0.6
    elif length <= 800:
        score += 0.35

    lowercase_text = stripped_text.lower()
    for keyword, weight in _SUPPLEMENT_TEXT_KEYWORDS:
        if keyword in lowercase_text:
            score += weight

    if index == 0 and score > 0:
        score += 0.25

    return score


def _should_preselect_chapter(
    title: str,
    text: str,
    index: int,
    total_count: int,
) -> bool:
    if total_count <= 1:
        return True
    score = _supplement_score(title, text, index)
    return score < 1.9


def _ensure_at_least_one_chapter_enabled(chapters: List[Dict[str, Any]]) -> None:
    if not chapters:
        return
    if any(chapter.get("enabled") for chapter in chapters):
        return
    best_index = max(range(len(chapters)), key=lambda idx: chapters[idx].get("characters", 0))
    chapters[best_index]["enabled"] = True


def _service() -> ConversionService:
    return current_app.extensions["conversion_service"]


def _require_pending_job(pending_id: str) -> PendingJob:
    pending = _service().get_pending_job(pending_id)
    if not pending:
        abort(404)
    return cast(PendingJob, pending)


def _build_voice_catalog() -> List[Dict[str, str]]:
    catalog: List[Dict[str, str]] = []
    gender_map = {"f": "Female", "m": "Male"}
    for voice_id in VOICES_INTERNAL:
        prefix, _, rest = voice_id.partition("_")
        language_code = prefix[0] if prefix else "a"
        gender_code = prefix[1] if len(prefix) > 1 else ""
        catalog.append(
            {
                "id": voice_id,
                "language": language_code,
                "language_label": LANGUAGE_DESCRIPTIONS.get(language_code, language_code.upper()),
                "gender": gender_map.get(gender_code, "Unknown"),
                "gender_code": gender_code,
                "display_name": rest.replace("_", " ").title() if rest else voice_id,
            }
        )
    return catalog


def _template_options() -> Dict[str, Any]:
    current_settings = _load_settings()
    profiles = serialize_profiles()
    ordered_profiles = sorted(profiles.items())
    profile_options = []
    for name, entry in ordered_profiles:
        profile_options.append(
            {
                "name": name,
                "language": (entry or {}).get("language", ""),
                "formula": _formula_from_profile(entry or {}) or "",
            }
        )
    voice_catalog = _build_voice_catalog()
    return {
        "languages": LANGUAGE_DESCRIPTIONS,
        "voices": VOICES_INTERNAL,
        "subtitle_formats": SUBTITLE_FORMATS,
        "supported_langs_for_subs": SUPPORTED_LANGUAGES_FOR_SUBTITLE_GENERATION,
        "output_formats": SUPPORTED_SOUND_FORMATS,
        "voice_profiles": ordered_profiles,
        "voice_profile_options": profile_options,
        "separate_formats": ["wav", "flac", "mp3", "opus"],
        "voice_catalog": voice_catalog,
        "voice_catalog_map": {entry["id"]: entry for entry in voice_catalog},
        "sample_voice_texts": SAMPLE_VOICE_TEXTS,
        "voice_profiles_data": profiles,
        "speaker_configs": list_configs(),
        "chunk_levels": _CHUNK_LEVEL_OPTIONS,
        "speaker_analysis_threshold": current_settings.get(
            "speaker_analysis_threshold", _DEFAULT_ANALYSIS_THRESHOLD
        ),
        "speaker_pronunciation_sentence": current_settings.get(
            "speaker_pronunciation_sentence", _settings_defaults()["speaker_pronunciation_sentence"]
        ),
    }


SAVE_MODE_LABELS = {
    "save_next_to_input": "Save next to input file",
    "save_to_desktop": "Save to Desktop",
    "choose_output_folder": "Choose output folder",
    "default_output": "Use default save location",
}

LEGACY_SAVE_MODE_MAP = {label: key for key, label in SAVE_MODE_LABELS.items()}

_APOSTROPHE_MODE_OPTIONS = [
    {"value": "off", "label": "Off"},
    {"value": "spacy", "label": "spaCy (built-in)"},
    {"value": "llm", "label": "LLM assisted"},
]

_LLM_CONTEXT_OPTIONS = [
    {"value": "sentence", "label": "Sentence only"},
]

BOOLEAN_SETTINGS = {
    "replace_single_newlines",
    "use_gpu",
    "save_chapters_separately",
    "merge_chapters_at_end",
    "save_as_project",
    "generate_epub3",
    "enable_entity_recognition",
    "auto_prefix_chapter_titles",
    "normalization_numbers",
    "normalization_titles",
    "normalization_terminal",
    "normalization_phoneme_hints",
}

FLOAT_SETTINGS = {"silence_between_chapters", "chapter_intro_delay", "llm_timeout"}
INT_SETTINGS = {"max_subtitle_words", "speaker_analysis_threshold"}


def _has_output_override() -> bool:
    return bool(os.environ.get("ABOGEN_OUTPUT_DIR") or os.environ.get("ABOGEN_OUTPUT_ROOT"))


def _settings_defaults() -> Dict[str, Any]:
    llm_env_defaults = environment_llm_defaults()
    return {
        "output_format": "wav",
        "subtitle_format": "srt",
        "save_mode": "default_output" if _has_output_override() else "save_next_to_input",
        "default_voice": VOICES_INTERNAL[0] if VOICES_INTERNAL else "",
        "replace_single_newlines": False,
        "use_gpu": True,
        "save_chapters_separately": False,
        "merge_chapters_at_end": True,
        "save_as_project": False,
        "separate_chapters_format": "wav",
        "silence_between_chapters": 2.0,
        "chapter_intro_delay": 0.5,
        "max_subtitle_words": 50,
        "chunk_level": "paragraph",
        "enable_entity_recognition": True,
        "generate_epub3": False,
        "auto_prefix_chapter_titles": True,
        "speaker_analysis_threshold": _DEFAULT_ANALYSIS_THRESHOLD,
        "speaker_pronunciation_sentence": "This is {{name}} speaking.",
        "speaker_random_languages": [],
    "llm_base_url": llm_env_defaults.get("llm_base_url", ""),
    "llm_api_key": llm_env_defaults.get("llm_api_key", ""),
    "llm_model": llm_env_defaults.get("llm_model", ""),
    "llm_timeout": llm_env_defaults.get("llm_timeout", 30.0),
    "llm_prompt": llm_env_defaults.get("llm_prompt", DEFAULT_LLM_PROMPT),
    "llm_context_mode": llm_env_defaults.get("llm_context_mode", "sentence"),
        "normalization_numbers": True,
        "normalization_titles": True,
        "normalization_terminal": True,
        "normalization_phoneme_hints": True,
        "normalization_apostrophe_mode": "spacy",
    }


def _llm_ready(settings: Mapping[str, Any]) -> bool:
    base_url = str(settings.get("llm_base_url") or "").strip()
    return bool(base_url)


_PROMPT_TOKEN_RE = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")


def _render_prompt_template(template: str, context: Mapping[str, str]) -> str:
    if not template:
        return ""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return context.get(key, "")

    return _PROMPT_TOKEN_RE.sub(_replace, template)


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"true", "1", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _coerce_float(value: Any, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 200) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _normalize_save_mode(value: Any, default: str) -> str:
    if isinstance(value, str):
        if value in SAVE_MODE_LABELS:
            return value
        if value in LEGACY_SAVE_MODE_MAP:
            return LEGACY_SAVE_MODE_MAP[value]
    return default


def _normalize_setting_value(key: str, value: Any, defaults: Dict[str, Any]) -> Any:
    if key in BOOLEAN_SETTINGS:
        return _coerce_bool(value, defaults[key])
    if key in FLOAT_SETTINGS:
        return _coerce_float(value, defaults[key])
    if key in INT_SETTINGS:
        return _coerce_int(value, defaults[key])
    if key == "save_mode":
        return _normalize_save_mode(value, defaults[key])
    if key == "output_format":
        return value if value in SUPPORTED_SOUND_FORMATS else defaults[key]
    if key == "subtitle_format":
        valid = {item[0] for item in SUBTITLE_FORMATS}
        return value if value in valid else defaults[key]
    if key == "separate_chapters_format":
        if isinstance(value, str):
            normalized = value.lower()
            if normalized in {"wav", "flac", "mp3", "opus"}:
                return normalized
        return defaults[key]
    if key == "default_voice":
        if isinstance(value, str) and value in VOICES_INTERNAL:
            return value
        return defaults[key]
    if key == "chunk_level":
        if isinstance(value, str) and value in _CHUNK_LEVEL_VALUES:
            return value
        return defaults[key]
    if key == "normalization_apostrophe_mode":
        if isinstance(value, str):
            normalized_mode = value.strip().lower()
            if normalized_mode in {"off", "spacy", "llm"}:
                return normalized_mode
        return defaults[key]
    if key == "llm_context_mode":
        if isinstance(value, str):
            normalized_scope = value.strip().lower()
            if normalized_scope == "sentence":
                return normalized_scope
        return defaults[key]
    if key == "llm_prompt":
        candidate = str(value or "").strip()
        return candidate if candidate else defaults[key]
    if key in {"llm_base_url", "llm_api_key", "llm_model"}:
        return str(value or "").strip()
    if key == "speaker_random_languages":
        if isinstance(value, (list, tuple, set)):
            return [code for code in value if isinstance(code, str) and code in LANGUAGE_DESCRIPTIONS]
        if isinstance(value, str):
            parts = [item.strip().lower() for item in value.split(",") if item.strip()]
            return [code for code in parts if code in LANGUAGE_DESCRIPTIONS]
        return defaults.get(key, [])
    return value if value is not None else defaults.get(key)


def _load_settings() -> Dict[str, Any]:
    defaults = _settings_defaults()
    cfg = load_config() or {}
    settings: Dict[str, Any] = {}
    for key, default in defaults.items():
        raw_value = cfg.get(key, default)
        settings[key] = _normalize_setting_value(key, raw_value, defaults)
    return settings

def _formula_from_profile(entry: Dict[str, Any]) -> Optional[str]:
    voices = entry.get("voices") or []
    if not voices:
        return None
    total = sum(weight for _, weight in voices)
    if total <= 0:
        return None

    def _format_weight(value: float) -> str:
        normalized = value / total if total else 0.0
        return (f"{normalized:.4f}").rstrip("0").rstrip(".") or "0"

    parts = [f"{name}*{_format_weight(weight)}" for name, weight in voices if weight > 0]
    return "+".join(parts) if parts else None


def _resolve_voice_choice(
    language: str,
    base_voice: str,
    profile_name: str,
    custom_formula: str,
    profiles: Dict[str, Any],
) -> tuple[str, str, Optional[str]]:
    resolved_voice = base_voice
    resolved_language = language
    selected_profile = None

    if profile_name:
        entry = profiles.get(profile_name)
        formula = _formula_from_profile(entry or {}) if entry else None
        if formula:
            resolved_voice = formula
            selected_profile = profile_name
            profile_language = (entry or {}).get("language")
            if profile_language:
                resolved_language = profile_language

    if custom_formula:
        resolved_voice = custom_formula
        selected_profile = None

    return resolved_voice, resolved_language, selected_profile


def _persist_cover_image(extraction_result: Any, stored_path: Path) -> tuple[Optional[Path], Optional[str]]:
    cover_bytes = getattr(extraction_result, "cover_image", None)
    if not cover_bytes:
        return None, None

    mime = getattr(extraction_result, "cover_mime", None)
    extension = mimetypes.guess_extension(mime or "") or ".png"
    base_stem = Path(stored_path).stem or "cover"
    candidate = stored_path.parent / f"{base_stem}_cover{extension}"
    counter = 1
    while candidate.exists():
        candidate = stored_path.parent / f"{base_stem}_cover_{counter}{extension}"
        counter += 1

    try:
        candidate.write_bytes(cover_bytes)
    except OSError:
        return None, None

    return candidate, mime


def _parse_voice_formula(formula: str) -> List[tuple[str, float]]:
    voices = parse_formula_terms(formula)
    total = sum(weight for _, weight in voices)
    if total <= 0:
        raise ValueError("Voice weights must sum to a positive value")
    return voices


def _sanitize_voice_entries(entries: Iterable[Any]) -> List[Dict[str, Any]]:
    sanitized: List[Dict[str, Any]] = []
    for entry in entries or []:
        if isinstance(entry, dict):
            voice_id = entry.get("id") or entry.get("voice")
            if not voice_id:
                continue
            enabled = entry.get("enabled", True)
            if not enabled:
                continue
            sanitized.append({"voice": voice_id, "weight": entry.get("weight")})
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            sanitized.append({"voice": entry[0], "weight": entry[1]})
    return sanitized


def _pairs_to_formula(pairs: Iterable[Tuple[str, float]]) -> Optional[str]:
    voices = [(voice, float(weight)) for voice, weight in pairs if float(weight) > 0]
    if not voices:
        return None
    total = sum(weight for _, weight in voices)
    if total <= 0:
        return None

    def _format_value(value: float) -> str:
        normalized = value / total if total else 0.0
        return (f"{normalized:.4f}").rstrip("0").rstrip(".") or "0"

    parts = [f"{voice}*{_format_value(weight)}" for voice, weight in voices]
    return "+".join(parts)


def _profiles_payload() -> Dict[str, Any]:
    return {"profiles": serialize_profiles()}


def _get_preview_pipeline(language: str, device: str):
    key = (language, device)
    with _preview_pipeline_lock:
        pipeline = _preview_pipelines.get(key)
        if pipeline is not None:
            return pipeline
        _, KPipeline = load_numpy_kpipeline()
        pipeline = KPipeline(lang_code=language, repo_id="hexgrad/Kokoro-82M", device=device)
        _preview_pipelines[key] = pipeline
        return pipeline


def _synthesize_audio_from_normalized(
    *,
    normalized_text: str,
    voice_spec: str,
    language: str,
    speed: float,
    use_gpu: bool,
    max_seconds: float,
) -> np.ndarray:
    if not normalized_text.strip():
        raise ValueError("Preview text is required")

    device = "cpu"
    if use_gpu:
        try:
            device = _select_device()
        except Exception:
            device = "cpu"
            use_gpu = False

    pipeline = _get_preview_pipeline(language, device)
    if pipeline is None:
        raise RuntimeError("Preview pipeline is unavailable")

    voice_choice: Any = voice_spec
    if voice_spec and "*" in voice_spec:
        voice_choice = get_new_voice(pipeline, voice_spec, use_gpu)

    segments = pipeline(
        normalized_text,
        voice=voice_choice,
        speed=speed,
        split_pattern=SPLIT_PATTERN,
    )

    audio_chunks: List[np.ndarray] = []
    accumulated = 0
    max_samples = int(max(1.0, max_seconds) * SAMPLE_RATE)

    for segment in segments:
        graphemes = getattr(segment, "graphemes", "").strip()
        if not graphemes:
            continue
        audio = _to_float32(getattr(segment, "audio", None))
        if audio.size == 0:
            continue
        remaining = max_samples - accumulated
        if remaining <= 0:
            break
        if audio.shape[0] > remaining:
            audio = audio[:remaining]
        audio_chunks.append(audio)
        accumulated += audio.shape[0]
        if accumulated >= max_samples:
            break

    if not audio_chunks:
        raise RuntimeError("Preview could not be generated")

    return np.concatenate(audio_chunks)


@web_bp.app_template_filter("datetimeformat")
def datetimeformat(value: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if not value:
        return "—"
    from datetime import datetime

    return datetime.fromtimestamp(value).strftime(fmt)


@web_bp.get("/")
def index() -> str:
    return render_template(
        "index.html",
        options=_template_options(),
        settings=_load_settings(),
    )


@web_bp.get("/queue")
def queue_page() -> ResponseReturnValue:
    return render_template(
        "queue.html",
        jobs_panel=_render_jobs_panel(),
    )


@web_bp.get("/find-books")
def find_books_page() -> ResponseReturnValue:
    # Potential integration target: Standard Ebooks OPDS feed
    # https://standardebooks.org/feeds/opds
    # Potential integration target: Project Gutenberg OPDS search
    # https://www.gutenberg.org/ebooks/search.opds/
    return render_template("find_books.html")


@web_bp.route("/settings", methods=["GET", "POST"])
def settings_page() -> ResponseReturnValue:
    options = _template_options()
    current_settings = _load_settings()

    if request.method == "POST":
        form = request.form
        defaults = _settings_defaults()
        updated: Dict[str, Any] = {}

        updated["output_format"] = _normalize_setting_value(
            "output_format", form.get("output_format"), defaults
        )
        updated["subtitle_format"] = _normalize_setting_value(
            "subtitle_format", form.get("subtitle_format"), defaults
        )
        updated["save_mode"] = _normalize_setting_value(
            "save_mode", form.get("save_mode"), defaults
        )
        updated["default_voice"] = _normalize_setting_value(
            "default_voice", form.get("default_voice"), defaults
        )
        for key in sorted(BOOLEAN_SETTINGS):
            updated[key] = _coerce_bool(form.get(key), False)
        updated["chunk_level"] = _normalize_setting_value(
            "chunk_level", form.get("chunk_level"), defaults
        )
        updated["separate_chapters_format"] = _normalize_setting_value(
            "separate_chapters_format", form.get("separate_chapters_format"), defaults
        )
        updated["silence_between_chapters"] = _coerce_float(
            form.get("silence_between_chapters"), defaults["silence_between_chapters"]
        )
        updated["chapter_intro_delay"] = _coerce_float(
            form.get("chapter_intro_delay"), defaults["chapter_intro_delay"]
        )
        updated["max_subtitle_words"] = _coerce_int(
            form.get("max_subtitle_words"), defaults["max_subtitle_words"]
        )
        updated["speaker_analysis_threshold"] = _coerce_int(
            form.get("speaker_analysis_threshold"),
            defaults["speaker_analysis_threshold"],
            minimum=1,
            maximum=25,
        )
        sentence_value = (form.get("speaker_pronunciation_sentence") or "").strip()
        if not sentence_value:
            sentence_value = defaults["speaker_pronunciation_sentence"]
        updated["speaker_pronunciation_sentence"] = sentence_value

        random_languages = [
            code.lower()
            for code in form.getlist("speaker_random_languages")
            if isinstance(code, str) and code.lower() in LANGUAGE_DESCRIPTIONS
        ]
        updated["speaker_random_languages"] = random_languages

        updated["llm_base_url"] = _normalize_setting_value(
            "llm_base_url", form.get("llm_base_url"), defaults
        )
        updated["llm_api_key"] = _normalize_setting_value(
            "llm_api_key", form.get("llm_api_key"), defaults
        )
        updated["llm_model"] = _normalize_setting_value("llm_model", form.get("llm_model"), defaults)
        updated["llm_prompt"] = _normalize_setting_value("llm_prompt", form.get("llm_prompt"), defaults)
        updated["llm_context_mode"] = _normalize_setting_value(
            "llm_context_mode", form.get("llm_context_mode"), defaults
        )
        updated["llm_timeout"] = _normalize_setting_value("llm_timeout", form.get("llm_timeout"), defaults)
        updated["normalization_apostrophe_mode"] = _normalize_setting_value(
            "normalization_apostrophe_mode",
            form.get("normalization_apostrophe_mode"),
            defaults,
        )

        cfg = load_config() or {}
        cfg.update(updated)
        save_config(cfg)
        clear_cached_settings()
        return redirect(url_for("web.settings_page", saved="1"))

    save_locations = [
        {"value": key, "label": label} for key, label in SAVE_MODE_LABELS.items()
    ]
    context = {
        "options": options,
        "settings": current_settings,
        "save_locations": save_locations,
        "default_output_dir": get_user_output_path(),
        "saved": request.args.get("saved") == "1",
        "apostrophe_modes": _APOSTROPHE_MODE_OPTIONS,
        "llm_context_options": _LLM_CONTEXT_OPTIONS,
        "llm_ready": _llm_ready(current_settings),
        "normalization_samples": NORMALIZATION_SAMPLE_TEXTS,
    }
    return render_template("settings.html", **context)


@api_bp.post("/llm/models")
def api_llm_models() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False) or {}
    current_settings = get_runtime_settings()

    base_url = str(payload.get("base_url") or payload.get("llm_base_url") or current_settings.get("llm_base_url") or "").strip()
    if not base_url:
        return jsonify({"error": "LLM base URL is required."}), 400

    api_key = str(payload.get("api_key") or payload.get("llm_api_key") or current_settings.get("llm_api_key") or "")
    timeout = _coerce_float(payload.get("timeout"), current_settings.get("llm_timeout", 30.0))

    overrides = {
        "llm_base_url": base_url,
        "llm_api_key": api_key,
        "llm_timeout": timeout,
    }

    merged = apply_normalization_overrides(current_settings, overrides)
    configuration = build_llm_configuration(merged)
    try:
        models = list_models(configuration)
    except LLMClientError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"models": models})


@api_bp.post("/llm/preview")
def api_llm_preview() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False) or {}
    sample_text = str(payload.get("text") or "").strip()
    if not sample_text:
        return jsonify({"error": "Text is required."}), 400

    base_settings = get_runtime_settings()
    overrides: Dict[str, Any] = {
        "llm_base_url": str(
            payload.get("base_url")
            or payload.get("llm_base_url")
            or base_settings.get("llm_base_url")
            or ""
        ).strip(),
        "llm_api_key": str(
            payload.get("api_key")
            or payload.get("llm_api_key")
            or base_settings.get("llm_api_key")
            or ""
        ),
        "llm_model": str(
            payload.get("model")
            or payload.get("llm_model")
            or base_settings.get("llm_model")
            or ""
        ),
        "llm_prompt": payload.get("prompt") or payload.get("llm_prompt") or base_settings.get("llm_prompt"),
        "llm_context_mode": payload.get("context_mode") or base_settings.get("llm_context_mode"),
        "llm_timeout": _coerce_float(payload.get("timeout"), base_settings.get("llm_timeout", 30.0)),
        "normalization_apostrophe_mode": "llm",
    }

    merged = apply_normalization_overrides(base_settings, overrides)
    if not merged.get("llm_base_url"):
        return jsonify({"error": "LLM base URL is required."}), 400
    if not merged.get("llm_model"):
        return jsonify({"error": "Select an LLM model before previewing."}), 400

    apostrophe_config = build_apostrophe_config(settings=merged)
    try:
        normalized_text = normalize_for_pipeline(sample_text, config=apostrophe_config, settings=merged)
    except LLMClientError as exc:
        return jsonify({"error": str(exc)}), 400

    context = {
        "text": sample_text,
        "normalized_text": normalized_text,
    }
    return jsonify(context)


@api_bp.post("/normalization/preview")
def api_normalization_preview() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False) or {}
    sample_text = str(payload.get("text") or "").strip()
    if not sample_text:
        return jsonify({"error": "Sample text is required."}), 400

    base_settings = get_runtime_settings()
    normalization_payload = payload.get("normalization") or {}
    overrides: Dict[str, Any] = {}

    boolean_keys = (
        "normalization_numbers",
        "normalization_titles",
        "normalization_terminal",
        "normalization_phoneme_hints",
    )
    for key in boolean_keys:
        if key in normalization_payload:
            overrides[key] = _coerce_bool(normalization_payload.get(key), base_settings.get(key, True))
    if "normalization_apostrophe_mode" in normalization_payload:
        overrides["normalization_apostrophe_mode"] = normalization_payload.get("normalization_apostrophe_mode")

    llm_payload = payload.get("llm") or {}
    for field in ("llm_base_url", "llm_api_key", "llm_model", "llm_prompt", "llm_context_mode"):
        if field in llm_payload:
            overrides[field] = llm_payload[field]
    if "llm_timeout" in llm_payload:
        overrides["llm_timeout"] = llm_payload.get("llm_timeout")

    merged = apply_normalization_overrides(base_settings, overrides)

    apostrophe_config = build_apostrophe_config(settings=merged)
    try:
        normalized_text = normalize_for_pipeline(sample_text, config=apostrophe_config, settings=merged)
    except LLMClientError as exc:
        return jsonify({"error": str(exc)}), 400

    voice_spec = str(payload.get("voice") or base_settings.get("default_voice") or "").strip()
    if not voice_spec and VOICES_INTERNAL:
        voice_spec = VOICES_INTERNAL[0]
    language = str(payload.get("language") or base_settings.get("language") or "a").strip() or "a"
    try:
        speed = float(payload.get("speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    try:
        max_seconds = max(1.0, min(15.0, float(payload.get("max_seconds", 8.0) or 8.0)))
    except (TypeError, ValueError):
        max_seconds = 8.0

    use_gpu_default = base_settings.get("use_gpu", True)
    use_gpu = _coerce_bool(payload.get("use_gpu"), use_gpu_default)

    try:
        audio_data = _synthesize_audio_from_normalized(
            normalized_text=normalized_text,
            voice_spec=voice_spec,
            language=language,
            speed=speed,
            use_gpu=use_gpu,
            max_seconds=max_seconds,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    buffer = io.BytesIO()
    sf.write(buffer, audio_data, SAMPLE_RATE, format="WAV")
    audio_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")

    return jsonify(
        {
            "normalized_text": normalized_text,
            "audio_base64": audio_base64,
            "sample_rate": SAMPLE_RATE,
        }
    )


@web_bp.get("/voices")
def voice_profiles_page() -> str:
    options = _template_options()
    return render_template("voices.html", options=options)


@web_bp.get("/entities")
def entities_page() -> ResponseReturnValue:
    options = _template_options()
    settings = _load_settings()
    languages_map = options.get("languages", {})

    raw_language = (request.args.get("lang") or settings.get("language") or "a").strip().lower()
    language = raw_language if raw_language in languages_map else "a"

    status_code = (request.args.get("status") or "").strip().lower()
    status_token = (request.args.get("token") or "").strip()
    status_error = (request.args.get("error") or "").strip()

    query = (request.args.get("q") or "").strip()
    voice_filter = (request.args.get("voice") or "all").strip().lower()
    pronunciation_filter = (request.args.get("pronunciation") or "all").strip().lower()
    limit_value = _coerce_int(request.args.get("limit"), 200, minimum=10, maximum=500)

    if query:
        overrides = search_pronunciation_overrides(language, query, limit=limit_value)
    else:
        overrides = all_pronunciation_overrides(language)
        if limit_value and len(overrides) > limit_value:
            overrides = overrides[:limit_value]

    display_rows: List[Dict[str, Any]] = []
    for entry in overrides:
        has_voice = bool((entry.get("voice") or "").strip())
        has_pronunciation = bool((entry.get("pronunciation") or "").strip())
        if voice_filter == "with-voice" and not has_voice:
            continue
        if voice_filter == "without-voice" and has_voice:
            continue
        if pronunciation_filter == "with-pronunciation" and not has_pronunciation:
            continue
        if pronunciation_filter == "without-pronunciation" and has_pronunciation:
            continue
        row = dict(entry)
        row["has_voice"] = has_voice
        row["has_pronunciation"] = has_pronunciation
        try:
            updated_dt = datetime.fromtimestamp(float(entry.get("updated_at") or 0))
            created_dt = datetime.fromtimestamp(float(entry.get("created_at") or 0))
        except (TypeError, ValueError):
            updated_dt = datetime.fromtimestamp(0)
            created_dt = datetime.fromtimestamp(0)
        row["updated_at_label"] = updated_dt.strftime("%Y-%m-%d %H:%M")
        row["created_at_label"] = created_dt.strftime("%Y-%m-%d %H:%M")
        display_rows.append(row)

    stats = {
        "total": len(overrides),
        "filtered": len(display_rows),
        "with_voice": sum(1 for row in display_rows if row["has_voice"]),
        "with_pronunciation": sum(1 for row in display_rows if row["has_pronunciation"]),
    }

    language_options = sorted(languages_map.items(), key=lambda item: item[1])
    voice_filters = [
        {"value": "all", "label": "All voices"},
        {"value": "with-voice", "label": "Assigned voice"},
        {"value": "without-voice", "label": "No voice"},
    ]
    pronunciation_filters = [
        {"value": "all", "label": "All pronunciations"},
        {"value": "with-pronunciation", "label": "Has pronunciation"},
        {"value": "without-pronunciation", "label": "No pronunciation"},
    ]

    status_message = ""
    if status_code == "saved":
        status_message = f"Updated override for {status_token or 'override'}."
    elif status_code == "deleted":
        status_message = f"Deleted override for {status_token or 'override'}."

    context = {
        "options": options,
        "language": language,
        "language_label": languages_map.get(language, language.upper()),
        "languages": language_options,
        "query": query,
        "voice_filter": voice_filter,
        "pronunciation_filter": pronunciation_filter,
        "voice_filter_options": voice_filters,
        "pronunciation_filter_options": pronunciation_filters,
        "limit": limit_value,
        "overrides": display_rows,
        "stats": stats,
        "status_message": status_message,
        "status_error": status_error,
    }
    return render_template("entities.html", **context)


@web_bp.post("/entities/override")
def entities_override_update() -> ResponseReturnValue:
    options = _template_options()
    languages_map = options.get("languages", {})

    raw_language = (request.form.get("lang") or "").strip().lower()
    language = raw_language if raw_language in languages_map else "a"

    token_value = (request.form.get("token") or "").strip()
    action = (request.form.get("action") or "save").strip().lower()
    pronunciation_value = (request.form.get("pronunciation") or "").strip()
    voice_value = (request.form.get("voice") or "").strip()
    notes_value = (request.form.get("notes") or "").strip()

    redirect_params: Dict[str, Any] = {"lang": language}
    state_mappings = (
        ("state_voice", "voice"),
        ("state_pronunciation", "pronunciation"),
        ("state_limit", "limit"),
        ("state_query", "q"),
    )
    for form_key, query_key in state_mappings:
        value = (request.form.get(form_key) or "").strip()
        if value:
            redirect_params[query_key] = value

    if not token_value:
        redirect_params["status"] = "error"
        redirect_params["error"] = "Missing override token."
        return redirect(url_for("web.entities_page", **redirect_params))

    status_code = "saved"
    try:
        if action == "delete":
            delete_pronunciation_override(language=language, token=token_value)
            status_code = "deleted"
        else:
            save_pronunciation_override(
                language=language,
                token=token_value,
                pronunciation=pronunciation_value or None,
                voice=voice_value or None,
                notes=notes_value or None,
                context=None,
            )
            status_code = "saved"
    except Exception as exc:  # pragma: no cover - defensive logging
        current_app.logger.exception("Failed to %s override for token %s", action, token_value)
        redirect_params["status"] = "error"
        redirect_params["error"] = "Failed to update override."
        return redirect(url_for("web.entities_page", **redirect_params))

    redirect_params["status"] = status_code
    redirect_params["token"] = token_value
    return redirect(url_for("web.entities_page", **redirect_params))


@api_bp.post("/entities/preview")
def api_entity_pronunciation_preview() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False) or {}
    token = str(payload.get("token") or "").strip()
    pronunciation = str(payload.get("pronunciation") or "").strip()
    if not token and not pronunciation:
        return jsonify({"error": "Provide a token or pronunciation to preview."}), 400

    settings = _load_settings()
    sample_template = settings.get("speaker_pronunciation_sentence", "This is {{name}} speaking.")
    spoken_label = pronunciation or token or ""
    preview_text = _render_prompt_template(sample_template, {"name": spoken_label, "token": token})
    if not preview_text.strip():
        preview_text = spoken_label or token
    if not preview_text:
        return jsonify({"error": "Unable to construct preview text."}), 400

    runtime_settings = get_runtime_settings()
    apostrophe_config = build_apostrophe_config(settings=runtime_settings)
    try:
        normalized_text = normalize_for_pipeline(preview_text, config=apostrophe_config, settings=runtime_settings)
    except LLMClientError as exc:
        return jsonify({"error": str(exc)}), 400

    voice_spec = str(payload.get("voice") or settings.get("default_voice") or "").strip()
    if not voice_spec and VOICES_INTERNAL:
        voice_spec = VOICES_INTERNAL[0]

    language = str(payload.get("language") or runtime_settings.get("language") or "a").strip() or "a"
    use_gpu = runtime_settings.get("use_gpu", True)
    max_seconds = 6.0
    try:
        preview_speed = float(payload.get("speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        preview_speed = 1.0
    try:
        audio_data = _synthesize_audio_from_normalized(
            normalized_text=normalized_text,
            voice_spec=voice_spec,
            language=language,
            speed=preview_speed,
            use_gpu=use_gpu,
            max_seconds=max_seconds,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500

    buffer = io.BytesIO()
    sf.write(buffer, audio_data, SAMPLE_RATE, format="WAV")
    audio_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")

    return jsonify(
        {
            "text": preview_text,
            "normalized_text": normalized_text,
            "audio_base64": audio_base64,
            "sample_rate": SAMPLE_RATE,
        }
    )


@web_bp.route("/speakers", methods=["GET", "POST"])
def speaker_configs_page() -> ResponseReturnValue:
    options = _template_options()
    configs = list_configs()
    message = None
    error = None

    if request.method == "POST":
        name, config_payload, errors = _extract_speaker_config_form(request.form)
        editing_payload = config_payload
        editing_name = name
        if errors:
            error = " ".join(errors)
            context = {
                "options": options,
                "configs": configs,
                "editing_name": editing_name,
                "editing": editing_payload,
                "message": message,
                "error": error,
            }
            return render_template("speakers.html", **context)
        upsert_config(name, config_payload)
        return redirect(url_for("web.speaker_configs_page", config=name, saved="1"))

    editing_name = request.args.get("config") or ""
    editing_payload = get_config(editing_name) if editing_name else None
    if editing_payload is None and configs:
        editing_name = configs[0]["name"]
        editing_payload = get_config(editing_name)
    if editing_payload is None:
        editing_payload = {
            "language": "a",
            "languages": [],
            "default_voice": "",
            "speakers": {},
            "notes": "",
            "version": 1,
        }

    if request.args.get("saved") == "1":
        message = "Speaker configuration saved."

    context = {
        "options": options,
        "configs": configs,
        "editing_name": editing_name,
        "editing": editing_payload,
        "message": message,
        "error": error,
    }
    return render_template("speakers.html", **context)


@web_bp.post("/speakers/<name>/delete")
def delete_speaker_config_route(name: str) -> ResponseReturnValue:
    delete_config(name)
    return redirect(url_for("web.speaker_configs_page"))


@web_bp.post("/voices")
def save_voice_profile_route() -> ResponseReturnValue:
    name = request.form.get("name", "").strip()
    language = request.form.get("language", "a").strip() or "a"
    formula = request.form.get("formula", "").strip()
    if not name or not formula:
        abort(400, "Name and formula are required")
    voices = _parse_voice_formula(formula)
    profiles = load_profiles()
    profiles[name] = {"voices": voices, "language": language}
    save_profiles(profiles)
    return redirect(url_for("web.voice_profiles_page"))


@web_bp.post("/voices/<name>/delete")
def delete_voice_profile_route(name: str) -> ResponseReturnValue:
    delete_profile(name)
    return redirect(url_for("web.voice_profiles_page"))


@api_bp.get("/voice-profiles")
def api_list_voice_profiles() -> ResponseReturnValue:
    return jsonify(_profiles_payload())


@api_bp.post("/voice-profiles")
def api_save_voice_profile() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False)
    name = (payload.get("name") or "").strip()
    if not name:
        abort(400, "Profile name is required")

    original = (payload.get("originalName") or "").strip()
    language = (payload.get("language") or "a").strip() or "a"
    formula = (payload.get("formula") or "").strip()

    try:
        if formula:
            voices = _parse_voice_formula(formula)
        else:
            voices_raw = _sanitize_voice_entries(payload.get("voices", []))
            voices = normalize_voice_entries(voices_raw)
        if not voices:
            raise ValueError("At least one voice must be enabled with a weight above zero")
        save_profile(name, language=language, voices=voices)
        if original and original != name:
            remove_profile(original)
    except ValueError as exc:
        abort(400, str(exc))

    return jsonify({"ok": True, "profile": name, **_profiles_payload()})


@api_bp.delete("/voice-profiles/<name>")
def api_delete_voice_profile(name: str) -> ResponseReturnValue:
    remove_profile(name)
    return jsonify({"ok": True, **_profiles_payload()})


@api_bp.post("/voice-profiles/<name>/duplicate")
def api_duplicate_voice_profile(name: str) -> ResponseReturnValue:
    payload = request.get_json(silent=True) or {}
    new_name = (payload.get("name") or payload.get("new_name") or "").strip()
    if not new_name:
        abort(400, "Duplicate name is required")
    duplicate_profile(name, new_name)
    return jsonify({"ok": True, "profile": new_name, **_profiles_payload()})


@api_bp.post("/voice-profiles/import")
def api_import_voice_profiles() -> ResponseReturnValue:
    replace = False
    data: Optional[Dict[str, Any]] = None
    if "file" in request.files:
        file_storage = request.files["file"]
        try:
            file_storage.stream.seek(0)
            raw_bytes = file_storage.read()
            text_payload = raw_bytes.decode("utf-8")
            data = json.loads(text_payload)
        except UnicodeDecodeError as exc:
            abort(400, f"JSON file must be UTF-8 encoded: {exc}")
        except Exception as exc:  # pragma: no cover - defensive
            abort(400, f"Invalid JSON file: {exc}")
        replace = request.form.get("replace_existing") in {"true", "1", "on"}
    else:
        payload = request.get_json(force=True, silent=False)
        replace = bool(payload.get("replace_existing", False))
        data = payload.get("profiles") or payload.get("data") or payload
        if not isinstance(data, dict):
            data = None
    if data is None:
        abort(400, "Import payload must be a dictionary")
    data_dict = cast(Dict[str, Any], data)
    imported: List[str] = []
    try:
        imported = import_profiles_data(data_dict, replace_existing=replace)
    except ValueError as exc:
        abort(400, str(exc))
    return jsonify({"ok": True, "imported": imported, **_profiles_payload()})


@api_bp.get("/voice-profiles/export")
def api_export_voice_profiles() -> ResponseReturnValue:
    names_param = request.args.get("names")
    names = None
    if names_param:
        names = [name.strip() for name in names_param.split(",") if name.strip()]
    payload = export_profiles_payload(names)
    buffer = io.BytesIO()
    buffer.write(json.dumps(payload, indent=2).encode("utf-8"))
    buffer.seek(0)
    filename = request.args.get("filename") or "voice_profiles.json"
    return send_file(
        buffer,
        mimetype="application/json",
        as_attachment=True,
        download_name=filename,
    )


@api_bp.post("/voice-profiles/preview")
def api_preview_voice_mix() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False)
    language = (payload.get("language") or "a").strip() or "a"
    text = (payload.get("text") or "").strip()
    speed = float(payload.get("speed", 1.0) or 1.0)
    try:
        requested_preview = float(payload.get("max_seconds", 60.0) or 60.0)
    except (TypeError, ValueError):
        requested_preview = 60.0
    max_seconds = max(1.0, min(60.0, requested_preview))
    profile_name = (payload.get("profile") or payload.get("profile_name") or "").strip()
    formula = (payload.get("formula") or "").strip()

    voices: List[Tuple[str, float]] = []
    if profile_name:
        profiles = load_profiles()
        entry = profiles.get(profile_name)
        if entry is None:
            abort(404, "Profile not found")
        if not isinstance(entry, dict):
            abort(400, "Profile data is invalid")
        entry_dict = cast(Dict[str, Any], entry)
        language = entry_dict.get("language", language)
        profile_voices = entry_dict.get("voices", [])
        for item in profile_voices:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    voices.append((str(item[0]), float(item[1])))
                except (TypeError, ValueError):
                    continue
    else:
        try:
            if formula:
                voices = _parse_voice_formula(formula)
            else:
                voices_raw = _sanitize_voice_entries(payload.get("voices", []))
                voices = normalize_voice_entries(voices_raw)
        except ValueError as exc:
            abort(400, str(exc))

    if not voices:
        abort(400, "At least one voice must be provided for preview")

    if not text:
        text = SAMPLE_VOICE_TEXTS.get(language, SAMPLE_VOICE_TEXTS.get("a", "This is a sample of the selected voice."))

    settings = _load_settings()
    use_gpu_default = settings.get("use_gpu", True)
    if "use_gpu" in payload:
        use_gpu = _coerce_bool(payload.get("use_gpu"), use_gpu_default)
    else:
        use_gpu = use_gpu_default
    device = "cpu"
    if use_gpu:
        try:
            device = _select_device()
        except Exception:  # pragma: no cover - fallback
            device = "cpu"
            use_gpu = False

    pipeline: Any = None
    try:
        pipeline = _get_preview_pipeline(language, device)
    except Exception as exc:  # pragma: no cover - defensive guard
        abort(500, f"Failed to initialise preview pipeline: {exc}")
    if pipeline is None:  # pragma: no cover - defensive double-check
        abort(500, "Preview pipeline initialisation failed")

    voice_choice: Any = None
    if len(voices) == 1:
        voice_choice = voices[0][0]
    else:
        formula_value = _pairs_to_formula(voices)
        if not formula_value:
            abort(400, "Invalid voice weights provided")
        try:
            voice_choice = get_new_voice(pipeline, formula_value, use_gpu)
        except ValueError as exc:
            abort(400, str(exc))
    if voice_choice is None:
        abort(400, "Unable to resolve voice selection")

    try:
        text = normalize_for_pipeline(text)
    except Exception:
        current_app.logger.exception("Voice preview normalization failed; using raw text")

    segments = pipeline(
        text,
        voice=voice_choice,
        speed=speed,
        split_pattern=SPLIT_PATTERN,
    )

    audio_chunks: List[np.ndarray] = []
    accumulated = 0
    max_samples = int(max_seconds * SAMPLE_RATE)

    for segment in segments:
        graphemes = segment.graphemes.strip()
        if not graphemes:
            continue
        audio = _to_float32(segment.audio)
        if audio.size == 0:
            continue
        remaining = max_samples - accumulated
        if remaining <= 0:
            break
        if audio.shape[0] > remaining:
            audio = audio[:remaining]
        audio_chunks.append(audio)
        accumulated += audio.shape[0]
        if accumulated >= max_samples:
            break

    if not audio_chunks:
        abort(500, "Preview could not be generated")

    audio_data = np.concatenate(audio_chunks)
    buffer = io.BytesIO()
    sf.write(buffer, audio_data, SAMPLE_RATE, format="WAV")
    buffer.seek(0)
    response = send_file(
        buffer,
        mimetype="audio/wav",
        as_attachment=False,
        download_name="voice_preview.wav",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.post("/speaker-preview")
def api_speaker_preview() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False)
    text = (payload.get("text") or "").strip()
    voice_spec = (payload.get("voice") or "").strip()
    language = (payload.get("language") or "a").strip() or "a"
    speed_input = payload.get("speed", 1.0)
    try:
        speed = float(speed_input)
    except (TypeError, ValueError):
        speed = 1.0
    max_seconds_input = payload.get("max_seconds", 8.0)
    try:
        max_seconds = max(1.0, min(15.0, float(max_seconds_input)))
    except (TypeError, ValueError):
        max_seconds = 8.0

    if not text:
        abort(400, "Preview text is required")
    if not voice_spec:
        abort(400, "Voice selection is required")

    settings = _load_settings()
    use_gpu_default = settings.get("use_gpu", True)
    if "use_gpu" in payload:
        use_gpu = _coerce_bool(payload.get("use_gpu"), use_gpu_default)
    else:
        use_gpu = use_gpu_default

    device = "cpu"
    if use_gpu:
        try:
            device = _select_device()
        except Exception:  # pragma: no cover - fallback
            device = "cpu"
            use_gpu = False

    try:
        pipeline = _get_preview_pipeline(language, device)
    except Exception as exc:  # pragma: no cover - defensive guard
        abort(500, f"Failed to initialise preview pipeline: {exc}")
    if pipeline is None:  # pragma: no cover - defensive double-check
        abort(500, "Preview pipeline initialisation failed")

    voice_choice: Any = voice_spec
    if "*" in voice_spec:
        try:
            voice_choice = get_new_voice(pipeline, voice_spec, use_gpu)
        except ValueError as exc:
            abort(400, str(exc))

    try:
        text = normalize_for_pipeline(text)
    except Exception:
        current_app.logger.exception("Preview normalization failed; using raw text")

    segments = pipeline(
        text,
        voice=voice_choice,
        speed=speed,
        split_pattern=SPLIT_PATTERN,
    )

    audio_chunks: List[np.ndarray] = []
    accumulated = 0
    max_samples = int(max_seconds * SAMPLE_RATE)

    for segment in segments:
        graphemes = getattr(segment, "graphemes", "").strip()
        if not graphemes:
            continue
        audio = _to_float32(getattr(segment, "audio", None))
        if audio.size == 0:
            continue
        remaining = max_samples - accumulated
        if remaining <= 0:
            break
        if audio.shape[0] > remaining:
            audio = audio[:remaining]
        audio_chunks.append(audio)
        accumulated += audio.shape[0]
        if accumulated >= max_samples:
            break

    if not audio_chunks:
        abort(500, "Preview could not be generated")

    audio_data = np.concatenate(audio_chunks)
    buffer = io.BytesIO()
    sf.write(buffer, audio_data, SAMPLE_RATE, format="WAV")
    buffer.seek(0)
    response = send_file(
        buffer,
        mimetype="audio/wav",
        as_attachment=False,
        download_name="speaker_preview.wav",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@api_bp.get("/pending/<pending_id>/entities")
def api_pending_entities(pending_id: str) -> ResponseReturnValue:
    pending = _require_pending_job(pending_id)
    refresh_flag = (request.args.get("refresh") or "").strip().lower()
    expected_cache = (request.args.get("cache_key") or "").strip()
    refresh_requested = refresh_flag in {"1", "true", "yes", "force"}
    if expected_cache and expected_cache != (pending.entity_cache_key or ""):
        refresh_requested = True
    if refresh_requested or not pending.entity_summary:
        _refresh_entity_summary(pending, pending.chapters)
        _service().store_pending_job(pending)
    return jsonify(_pending_entities_payload(pending))


@api_bp.post("/pending/<pending_id>/entities/refresh")
def api_refresh_pending_entities(pending_id: str) -> ResponseReturnValue:
    pending = _require_pending_job(pending_id)
    _refresh_entity_summary(pending, pending.chapters)
    _service().store_pending_job(pending)
    return jsonify(_pending_entities_payload(pending))


@api_bp.get("/pending/<pending_id>/manual-overrides")
def api_list_manual_overrides(pending_id: str) -> ResponseReturnValue:
    pending = _require_pending_job(pending_id)
    return jsonify(
        {
            "overrides": pending.manual_overrides or [],
            "pronunciation_overrides": pending.pronunciation_overrides or [],
            "language": pending.language or "en",
        }
    )


@api_bp.post("/pending/<pending_id>/manual-overrides")
def api_upsert_manual_override(pending_id: str) -> ResponseReturnValue:
    pending = _require_pending_job(pending_id)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, Mapping):
        abort(400, "Invalid override payload")
    try:
        override = _upsert_manual_override(pending, payload)
    except ValueError as exc:
        abort(400, str(exc))
    _service().store_pending_job(pending)
    return jsonify({"override": override, **_pending_entities_payload(pending)})


@api_bp.delete("/pending/<pending_id>/manual-overrides/<override_id>")
def api_delete_manual_override(pending_id: str, override_id: str) -> ResponseReturnValue:
    pending = _require_pending_job(pending_id)
    deleted = _delete_manual_override(pending, override_id)
    if not deleted:
        abort(404)
    _service().store_pending_job(pending)
    return jsonify({"deleted": True, **_pending_entities_payload(pending)})


@api_bp.get("/pending/<pending_id>/manual-overrides/search")
def api_search_manual_override_candidates(pending_id: str) -> ResponseReturnValue:
    pending = _require_pending_job(pending_id)
    query = (request.args.get("q") or request.args.get("query") or "").strip()
    limit_param = request.args.get("limit")
    limit_value = _coerce_int(limit_param, 15, minimum=1, maximum=50) if limit_param is not None else 15
    results = _search_manual_override_candidates(pending, query, limit=limit_value)
    return jsonify({"query": query, "limit": limit_value, "results": results})


@web_bp.post("/jobs")
def enqueue_job() -> ResponseReturnValue:
    service = _service()
    uploads_dir = Path(current_app.config["UPLOAD_FOLDER"])
    uploads_dir.mkdir(parents=True, exist_ok=True)

    file = request.files.get("source_file")
    text_input = request.form.get("source_text", "").strip()
    pending_id = (request.form.get("pending_id") or "").strip()

    settings = _load_settings()
    profiles = load_profiles()

    if pending_id and not file and not text_input:
        pending = service.get_pending_job(pending_id)
        if not pending:
            abort(404, "Pending job not found")
        previous_language = pending.language
        _apply_book_step_form(pending, request.form, settings=settings, profiles=profiles)
        setattr(pending, "analysis_requested", False)
        if pending.language != previous_language:
            _refresh_entity_summary(pending, pending.chapters)
        service.store_pending_job(pending)
        if _wants_wizard_json():
            return _wizard_json_response(pending, "chapters")
        return redirect(url_for("web.index"))

    if not file and not text_input:
        return redirect(url_for("web.index"))

    stored_path: Path
    original_name: str

    if file and file.filename:
        filename = secure_filename(file.filename)
        if not filename:
            return redirect(url_for("web.index"))
        stored_path = uploads_dir / f"{uuid.uuid4().hex}_{filename}"
        file.save(stored_path)
        original_name = filename
    else:
        original_name = "direct_text.txt"
        stored_path = uploads_dir / f"{uuid.uuid4().hex}_{original_name}"
        stored_path.write_text(text_input, encoding="utf-8")

    extraction = None
    try:
        extraction = extract_from_path(stored_path)
    except Exception as exc:  # pragma: no cover - defensive
        try:
            stored_path.unlink(missing_ok=True)
        except Exception:
            pass
        abort(400, f"Unable to read the supplied content: {exc}")

    if extraction is None:  # pragma: no cover - defensive
        abort(400, "Unable to read the supplied content")

    assert extraction is not None

    cover_path, cover_mime = _persist_cover_image(extraction, stored_path)

    if extraction.chapters:
        original_titles = [chapter.title for chapter in extraction.chapters]
        normalized_titles = normalize_roman_numeral_titles(original_titles)
        if normalized_titles != original_titles:
            for chapter, new_title in zip(extraction.chapters, normalized_titles):
                chapter.title = new_title

    metadata_tags = extraction.metadata or {}
    total_chars = extraction.total_characters or calculate_text_length(extraction.combined_text)
    total_chapter_count = len(extraction.chapters)
    chapters_payload: List[Dict[str, Any]] = []
    for index, chapter in enumerate(extraction.chapters):
        enabled = _should_preselect_chapter(
            chapter.title,
            chapter.text,
            index,
            total_chapter_count,
        )
        chapters_payload.append(
            {
                "id": f"{index:04d}",
                "index": index,
                "title": chapter.title,
                "text": chapter.text,
                "characters": calculate_text_length(chapter.text),
                "enabled": enabled,
            }
        )

    if not chapters_payload:
        chapters_payload.append(
            {
                "id": "0000",
                "index": 0,
                "title": original_name,
                "text": "",
                "characters": 0,
                "enabled": True,
            }
        )

    _ensure_at_least_one_chapter_enabled(chapters_payload)

    language = request.form.get("language", "a")
    base_voice = request.form.get("voice", "af_alloy")
    profile_selection = (request.form.get("voice_profile") or "__standard").strip()
    custom_formula_raw = request.form.get("voice_formula", "").strip()
    selected_speaker_config = (request.form.get("speaker_config") or "").strip()
    speaker_config_payload = get_config(selected_speaker_config) if selected_speaker_config else None

    if profile_selection in {"__standard", ""}:
        profile_name = ""
        custom_formula = ""
    elif profile_selection == "__formula":
        profile_name = ""
        custom_formula = custom_formula_raw
    else:
        profile_name = profile_selection
        custom_formula = ""

    voice, language, selected_profile = _resolve_voice_choice(
        language,
        base_voice,
        profile_name,
        custom_formula,
        profiles,
    )
    speed = float(request.form.get("speed", "1.0"))
    subtitle_mode = request.form.get("subtitle_mode", "Disabled")
    output_format = settings["output_format"]
    subtitle_format = settings["subtitle_format"]
    save_mode_key = settings["save_mode"]
    save_mode = SAVE_MODE_LABELS.get(save_mode_key, SAVE_MODE_LABELS["save_next_to_input"])
    replace_single_newlines = settings["replace_single_newlines"]
    use_gpu = settings["use_gpu"]
    save_chapters_separately = settings["save_chapters_separately"]
    merge_chapters_at_end = settings["merge_chapters_at_end"] or not save_chapters_separately
    save_as_project = settings["save_as_project"]
    separate_chapters_format = settings["separate_chapters_format"]
    silence_between_chapters = settings["silence_between_chapters"]
    chapter_intro_delay = settings["chapter_intro_delay"]
    max_subtitle_words = settings["max_subtitle_words"]
    auto_prefix_chapter_titles = settings["auto_prefix_chapter_titles"]

    chunk_level_default = str(settings.get("chunk_level", "paragraph")).strip().lower()
    raw_chunk_level = (request.form.get("chunk_level") or chunk_level_default).strip().lower()
    if raw_chunk_level not in _CHUNK_LEVEL_VALUES:
        raw_chunk_level = chunk_level_default if chunk_level_default in _CHUNK_LEVEL_VALUES else "paragraph"
    chunk_level_value = raw_chunk_level
    chunk_level_literal = cast(ChunkLevel, chunk_level_value)

    speaker_mode_value = "single"

    generate_epub3_default = bool(settings.get("generate_epub3", False))
    generate_epub3 = _coerce_bool(request.form.get("generate_epub3"), generate_epub3_default)

    selected_chapter_sources = [entry for entry in chapters_payload if entry.get("enabled")]
    raw_chunks = build_chunks_for_chapters(selected_chapter_sources, level=chunk_level_literal)
    analysis_chunks = build_chunks_for_chapters(selected_chapter_sources, level="sentence")

    analysis_threshold = _coerce_int(
        settings.get("speaker_analysis_threshold"),
        _DEFAULT_ANALYSIS_THRESHOLD,
        minimum=1,
        maximum=25,
    )

    initial_analysis = False
    processed_chunks, speakers, analysis_payload, config_languages, _ = _prepare_speaker_metadata(
        chapters=selected_chapter_sources,
        chunks=raw_chunks,
        analysis_chunks=analysis_chunks,
        voice=voice,
        voice_profile=selected_profile or None,
        threshold=analysis_threshold,
        run_analysis=initial_analysis,
        speaker_config=speaker_config_payload,
        apply_config=bool(speaker_config_payload),
    )

    pending = PendingJob(
        id=uuid.uuid4().hex,
        original_filename=original_name,
        stored_path=stored_path,
        language=language,
        voice=voice,
        speed=speed,
        use_gpu=use_gpu,
        subtitle_mode=subtitle_mode,
        output_format=output_format,
        save_mode=save_mode,
        output_folder=None,
        replace_single_newlines=replace_single_newlines,
        subtitle_format=subtitle_format,
        total_characters=total_chars,
        save_chapters_separately=save_chapters_separately,
        merge_chapters_at_end=merge_chapters_at_end,
        separate_chapters_format=separate_chapters_format,
        silence_between_chapters=silence_between_chapters,
        save_as_project=save_as_project,
        voice_profile=selected_profile or None,
        max_subtitle_words=max_subtitle_words,
        metadata_tags=metadata_tags,
        chapters=chapters_payload,
        created_at=time.time(),
        cover_image_path=cover_path,
        cover_image_mime=cover_mime,
        chapter_intro_delay=chapter_intro_delay,
        auto_prefix_chapter_titles=bool(auto_prefix_chapter_titles),
        chunk_level=chunk_level_value,
        speaker_mode=speaker_mode_value,
        generate_epub3=generate_epub3,
        chunks=processed_chunks,
        speakers=speakers,
        speaker_analysis=analysis_payload,
        speaker_analysis_threshold=analysis_threshold,
        analysis_requested=initial_analysis,
    )

    _refresh_entity_summary(pending, pending.chapters)

    service.store_pending_job(pending)
    pending.applied_speaker_config = selected_speaker_config or None
    if config_languages:
        pending.speaker_voice_languages = list(config_languages)
    elif isinstance(speaker_config_payload, Mapping):
        languages = speaker_config_payload.get("languages")
        if isinstance(languages, list):
            pending.speaker_voice_languages = [code for code in languages if isinstance(code, str)]
    if _wants_wizard_json():
        return _wizard_json_response(pending, "chapters")
    return redirect(url_for("web.index"))


@web_bp.get("/jobs/prepare/<pending_id>")
def prepare_job(pending_id: str) -> ResponseReturnValue:
    pending = _require_pending_job(pending_id)
    requested_step = request.args.get("step") or "chapters"
    normalized_step = _normalize_wizard_step(requested_step, pending)
    if _wants_wizard_json():
        return _wizard_json_response(pending, normalized_step)
    return redirect(url_for("web.index"))


@web_bp.post("/jobs/prepare/<pending_id>/analyze")
def analyze_pending_job(pending_id: str) -> ResponseReturnValue:
    service = _service()
    pending = _require_pending_job(pending_id)

    (
        chunk_level_literal,
        overrides,
        enabled_overrides,
        errors,
        selected_total,
        selected_config,
        apply_config_requested,
        persist_config_requested,
    ) = _apply_prepare_form(pending, request.form)

    if errors:
        message = " ".join(errors)
        if _wants_wizard_json():
            return _wizard_json_response(
                pending,
                "chapters",
                error=message,
                status=400,
            )
        abort(400, message)

    if not enabled_overrides:
        setattr(pending, "analysis_requested", False)
        pending.chunks = []
        pending.speaker_analysis = {}
        error_message = "Select at least one chapter to analyze."
        if _wants_wizard_json():
            return _wizard_json_response(
                pending,
                "chapters",
                error=error_message,
                status=400,
            )
        abort(400, error_message)

    raw_chunks = build_chunks_for_chapters(enabled_overrides, level=chunk_level_literal)
    analysis_chunks = build_chunks_for_chapters(enabled_overrides, level="sentence")

    existing_roster: Optional[Mapping[str, Any]]
    if getattr(pending, "analysis_requested", False):
        existing_roster = pending.speakers
    else:
        existing_roster = None

    config_name = pending.applied_speaker_config or selected_config
    speaker_config_payload = get_config(config_name) if config_name else None
    processed_chunks, roster, analysis_payload, config_languages, updated_config = _prepare_speaker_metadata(
        chapters=enabled_overrides,
        chunks=raw_chunks,
        analysis_chunks=analysis_chunks,
        voice=pending.voice,
        voice_profile=pending.voice_profile,
        threshold=pending.speaker_analysis_threshold,
        existing_roster=existing_roster,
        run_analysis=True,
        speaker_config=speaker_config_payload,
        apply_config=apply_config_requested or bool(speaker_config_payload),
        persist_config=persist_config_requested,
    )

    pending.chunks = processed_chunks
    pending.speakers = roster
    pending.speaker_analysis = analysis_payload
    if config_languages:
        pending.speaker_voice_languages = list(config_languages)
    config_name = getattr(pending, "applied_speaker_config", None)
    if updated_config and isinstance(config_name, str) and config_name:
        configs = load_configs()
        configs[config_name] = updated_config
        save_configs(configs)
    setattr(pending, "analysis_requested", True)
    if selected_total:
        pending.total_characters = selected_total

    _refresh_entity_summary(pending, enabled_overrides)
    _sync_pronunciation_overrides(pending)

    service.store_pending_job(pending)

    notice_message = "Entity insights updated."
    if persist_config_requested and config_name:
        notice_message = "Entity insights updated and configuration saved."
    if _wants_wizard_json():
        return _wizard_json_response(
            pending,
            "entities",
            notice=notice_message,
        )
    return redirect(url_for("web.index"))


@web_bp.post("/jobs/prepare/<pending_id>")
def finalize_job(pending_id: str) -> ResponseReturnValue:
    service = _service()
    pending = _require_pending_job(pending_id)

    (
        chunk_level_literal,
        overrides,
        enabled_overrides,
        errors,
        selected_total,
        selected_config,
        apply_config_requested,
        persist_config_requested,
    ) = _apply_prepare_form(pending, request.form)

    if errors:
        active_hint = request.form.get("active_step") or "entities"
        normalized_step = _normalize_wizard_step(active_hint, pending)
        message = " ".join(errors)
        if _wants_wizard_json():
            return _wizard_json_response(
                pending,
                normalized_step,
                error=message,
                status=400,
            )
        abort(400, message)

    if not enabled_overrides:
        pending.chunks = []
        error_message = "Select at least one chapter to convert."
        if _wants_wizard_json():
            return _wizard_json_response(
                pending,
                "chapters",
                error=error_message,
                status=400,
            )
        abort(400, error_message)

    active_step = (request.form.get("active_step") or "chapters").strip().lower()
    if active_step == "speakers":
        active_step = "entities"

    normalized_step = _normalize_wizard_step(active_step, pending)
    raw_chunks = build_chunks_for_chapters(enabled_overrides, level=chunk_level_literal)
    analysis_chunks = build_chunks_for_chapters(enabled_overrides, level="sentence")
    analysis_requested = bool(getattr(pending, "analysis_requested", False))
    should_force_entities = analysis_requested and normalized_step != "entities"

    if analysis_requested:
        existing_roster: Optional[Mapping[str, Any]] = pending.speakers
    else:
        narrator_only: Dict[str, Any] = {}
        if isinstance(pending.speakers, dict):
            narrator_payload = pending.speakers.get("narrator")
            if isinstance(narrator_payload, Mapping):
                narrator_only["narrator"] = dict(narrator_payload)
        existing_roster = narrator_only or None

    config_name = pending.applied_speaker_config or selected_config
    speaker_config_payload = get_config(config_name) if config_name else None
    run_analysis = should_force_entities or analysis_requested
    processed_chunks, roster, analysis_payload, config_languages, updated_config = _prepare_speaker_metadata(
        chapters=enabled_overrides,
        chunks=raw_chunks,
        analysis_chunks=analysis_chunks,
        voice=pending.voice,
        voice_profile=pending.voice_profile,
        threshold=pending.speaker_analysis_threshold,
        existing_roster=existing_roster,
        run_analysis=run_analysis,
        speaker_config=speaker_config_payload,
        apply_config=apply_config_requested or bool(speaker_config_payload),
        persist_config=persist_config_requested,
    )

    pending.chunks = processed_chunks
    pending.speakers = roster
    if analysis_payload:
        pending.speaker_analysis = analysis_payload
    if run_analysis:
        setattr(pending, "analysis_requested", True)

    if config_languages:
        pending.speaker_voice_languages = list(config_languages)
    config_key = getattr(pending, "applied_speaker_config", None)
    if updated_config and isinstance(config_key, str) and config_key:
        configs = load_configs()
        configs[config_key] = updated_config
        save_configs(configs)

    if selected_total:
        pending.total_characters = selected_total

    _refresh_entity_summary(pending, enabled_overrides)
    _sync_pronunciation_overrides(pending)

    requested_step = normalized_step
    should_render_entities = should_force_entities or requested_step == "entities"
    if should_render_entities:
        notice_message = ""
        if should_force_entities:
            notice_message = "Review entity settings before queuing."
            if persist_config_requested and config_key:
                notice_message = "Configuration saved. Review entity settings before queuing."
        elif persist_config_requested and config_key:
            notice_message = "Configuration saved."
        service.store_pending_job(pending)
        if _wants_wizard_json():
            return _wizard_json_response(
                pending,
                "entities",
                notice=notice_message or None,
            )
        return redirect(url_for("web.index"))

    total_characters = selected_total or pending.total_characters
    service.pop_pending_job(pending_id)

    job = service.enqueue(
        original_filename=pending.original_filename,
        stored_path=pending.stored_path,
        language=pending.language,
        voice=pending.voice,
        speed=pending.speed,
        use_gpu=pending.use_gpu,
        subtitle_mode=pending.subtitle_mode,
        output_format=pending.output_format,
        save_mode=pending.save_mode,
        output_folder=pending.output_folder,
        replace_single_newlines=pending.replace_single_newlines,
        subtitle_format=pending.subtitle_format,
        total_characters=total_characters,
        chapters=overrides,
        save_chapters_separately=pending.save_chapters_separately,
        merge_chapters_at_end=pending.merge_chapters_at_end,
        separate_chapters_format=pending.separate_chapters_format,
        silence_between_chapters=pending.silence_between_chapters,
        save_as_project=pending.save_as_project,
        voice_profile=pending.voice_profile,
        max_subtitle_words=pending.max_subtitle_words,
        metadata_tags=pending.metadata_tags,
        cover_image_path=pending.cover_image_path,
        cover_image_mime=pending.cover_image_mime,
        chapter_intro_delay=pending.chapter_intro_delay,
        auto_prefix_chapter_titles=getattr(pending, "auto_prefix_chapter_titles", True),
        chunk_level=pending.chunk_level,
        chunks=processed_chunks,
        speakers=roster,
        speaker_mode=pending.speaker_mode,
        generate_epub3=pending.generate_epub3,
        speaker_analysis=pending.speaker_analysis,
        speaker_analysis_threshold=pending.speaker_analysis_threshold,
        analysis_requested=getattr(pending, "analysis_requested", False),
        entity_summary=pending.entity_summary,
        manual_overrides=pending.manual_overrides,
        pronunciation_overrides=pending.pronunciation_overrides,
    )

    if config_languages:
        job.speaker_voice_languages = list(config_languages)
    elif pending.speaker_voice_languages:
        job.speaker_voice_languages = list(pending.speaker_voice_languages)

    if isinstance(config_key, str) and config_key:
        job.applied_speaker_config = config_key

    redirect_url = url_for("web.index", _anchor="queue")
    if _wants_wizard_json():
        return jsonify({"redirect_url": redirect_url})
    return redirect(redirect_url)


@web_bp.post("/jobs/prepare/<pending_id>/cancel")
def cancel_pending_job(pending_id: str) -> ResponseReturnValue:
    pending = _service().pop_pending_job(pending_id)
    if pending and pending.stored_path.exists():
        try:
            pending.stored_path.unlink()
        except OSError:
            pass
    if pending and pending.cover_image_path and pending.cover_image_path.exists():
        try:
            pending.cover_image_path.unlink()
        except OSError:
            pass
    redirect_url = url_for("web.index", _anchor="queue")
    if _wants_wizard_json():
        return jsonify({"cancelled": True, "redirect_url": redirect_url})
    return redirect(redirect_url)


def _render_jobs_panel() -> str:
    jobs = _service().list_jobs()
    active_statuses = {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED}
    active_jobs = [job for job in jobs if job.status in active_statuses]
    active_jobs.sort(key=lambda job: ((job.queue_position or 10_000), -job.created_at))
    finished_jobs = [job for job in jobs if job.status not in active_statuses]
    download_flags = {job.id: _job_download_flags(job) for job in jobs}
    return render_template(
        "partials/jobs.html",
        active_jobs=active_jobs,
        finished_jobs=finished_jobs[:5],
        total_finished=len(finished_jobs),
        JobStatus=JobStatus,
        download_flags=download_flags,
    )


def _normalize_wizard_step(step: Optional[str], pending: Optional[PendingJob] = None) -> str:
    if pending is None:
        default_step = "book"
    else:
        default_step = "chapters"
    if not step:
        chosen = default_step
    else:
        normalized = step.strip().lower()
        if normalized in {"", "upload", "settings"}:
            chosen = default_step
        elif normalized == "speakers":
            chosen = "entities"
        elif normalized in _WIZARD_STEP_ORDER:
            chosen = normalized
        else:
            chosen = default_step
    return chosen


def _wants_wizard_json() -> bool:
    format_hint = request.args.get("format", "").strip().lower()
    if format_hint == "json":
        return True
    accept_header = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept_header:
        return True
    requested_with = (request.headers.get("X-Requested-With") or "").lower()
    if requested_with in {"xmlhttprequest", "fetch"}:
        return True
    wizard_header = (request.headers.get("X-Abogen-Wizard") or "").lower()
    return wizard_header == "json"


def _render_wizard_partial(
    pending: Optional[PendingJob],
    step: str,
    *,
    error: Optional[str] = None,
    notice: Optional[str] = None,
) -> str:
    templates = {
        "book": "partials/new_job_step_book.html",
        "chapters": "partials/new_job_step_chapters.html",
        "entities": "partials/new_job_step_entities.html",
    }
    template_name = templates[step]
    context: Dict[str, Any] = {
        "pending": pending,
        "readonly": False,
        "options": _template_options(),
        "settings": _load_settings(),
        "error": error,
        "notice": notice,
    }
    return render_template(template_name, **context)


def _wizard_step_payload(
    pending: Optional[PendingJob],
    step: str,
    html: str,
    *,
    error: Optional[str] = None,
    notice: Optional[str] = None,
) -> Dict[str, Any]:
    meta = _WIZARD_STEP_META.get(step, {})
    try:
        active_index = _WIZARD_STEP_ORDER.index(step)
    except ValueError:
        active_index = 0
    max_recorded_index = active_index
    if pending is not None:
        stored_index = int(getattr(pending, "wizard_max_step_index", -1))
        if stored_index < 0:
            stored_index = -1
        max_recorded_index = max(active_index, stored_index)
        max_allowed = len(_WIZARD_STEP_ORDER) - 1
        if max_recorded_index > max_allowed:
            max_recorded_index = max_allowed
        if stored_index != max_recorded_index:
            pending.wizard_max_step_index = max_recorded_index
            _service().store_pending_job(pending)
    else:
        max_allowed = len(_WIZARD_STEP_ORDER) - 1
        if max_recorded_index > max_allowed:
            max_recorded_index = max_allowed
    completed = [slug for idx, slug in enumerate(_WIZARD_STEP_ORDER) if idx <= max_recorded_index]
    return {
        "step": step,
        "step_index": int(meta.get("index", active_index + 1)),
        "total_steps": len(_WIZARD_STEP_ORDER),
        "title": meta.get("title", ""),
        "hint": meta.get("hint", ""),
        "html": html,
        "completed_steps": completed,
        "pending_id": pending.id if pending else "",
        "filename": pending.original_filename if pending and pending.original_filename else "",
        "error": error or "",
        "notice": notice or "",
    }


def _wizard_json_response(
    pending: Optional[PendingJob],
    step: str,
    *,
    error: Optional[str] = None,
    notice: Optional[str] = None,
    status: int = 200,
) -> ResponseReturnValue:
    html = _render_wizard_partial(pending, step, error=error, notice=notice)
    payload = _wizard_step_payload(pending, step, html, error=error, notice=notice)
    return jsonify(payload), status


@web_bp.get("/jobs/<job_id>")
def job_detail(job_id: str) -> str:
    job = _service().get_job(job_id)
    if not job:
        abort(404)
    return render_template(
        "job_detail.html",
        job=job,
        options=_template_options(),
        JobStatus=JobStatus,
        downloads=_job_download_flags(job),
    )


@web_bp.post("/jobs/<job_id>/pause")
def pause_job(job_id: str) -> ResponseReturnValue:
    _service().pause(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/resume")
def resume_job(job_id: str) -> ResponseReturnValue:
    _service().resume(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str) -> ResponseReturnValue:
    _service().cancel(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/delete")
def delete_job(job_id: str) -> ResponseReturnValue:
    _service().delete(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.index"))


@web_bp.post("/jobs/<job_id>/retry")
def retry_job(job_id: str) -> ResponseReturnValue:
    new_job = _service().retry(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    if new_job:
        return redirect(url_for("web.job_detail", job_id=new_job.id))
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/clear-finished")
def clear_finished_jobs() -> ResponseReturnValue:
    _service().clear_finished()
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.index", _anchor="queue"))


@web_bp.get("/jobs/<job_id>/epub")
def job_epub(job_id: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    epub_path = _locate_job_epub(job)
    if not epub_path:
        abort(404)
    return send_file(
        epub_path,
        mimetype="application/epub+zip",
        as_attachment=False,
        download_name=epub_path.name,
        conditional=True,
    )


@web_bp.get("/jobs/<job_id>/audio-stream")
def job_audio_stream(job_id: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    audio_path = _locate_job_audio(job)
    if not audio_path:
        abort(404)
    mime_type, _ = mimetypes.guess_type(str(audio_path))
    return send_file(
        audio_path,
        mimetype=mime_type or "audio/mpeg",
        as_attachment=False,
        conditional=True,
    )


@web_bp.get("/jobs/<job_id>/reader")
def job_reader(job_id: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    epub_path = _locate_job_epub(job)
    if not epub_path:
        abort(404)
    chapters = _extract_epub_chapters(epub_path)
    audio_path = _locate_job_audio(job)
    chapter_url = url_for("web.job_reader_chapter", job_id=job.id)
    asset_base = url_for("web.job_reader_asset", job_id=job.id, asset_path="").rstrip("/") + "/"
    audio_url = url_for("web.job_audio_stream", job_id=job.id) if audio_path else ""
    epub_url = url_for("web.job_epub", job_id=job.id)
    metadata_payload = _load_job_metadata(job)
    metadata_section_raw = metadata_payload.get("metadata") if isinstance(metadata_payload, Mapping) else {}
    metadata_section = metadata_section_raw if isinstance(metadata_section_raw, Mapping) else {}
    job_metadata = job.metadata_tags if isinstance(job.metadata_tags, Mapping) else {}
    display_title = _resolve_book_title(job, metadata_section, job_metadata)

    timing_map: Dict[int, Dict[str, Any]] = {}
    chapter_entries = metadata_payload.get("chapters") if isinstance(metadata_payload, Mapping) else []
    for entry in chapter_entries or []:
        if not isinstance(entry, Mapping):
            continue
        index_raw = entry.get("index")
        index_value: Optional[int]
        if isinstance(index_raw, (int, float)) and not isinstance(index_raw, bool):
            index_value = int(index_raw) - 1
        elif isinstance(index_raw, str):
            stripped = index_raw.strip()
            if not stripped:
                continue
            try:
                index_value = int(stripped) - 1
            except ValueError:
                continue
        else:
            continue
        if index_value < 0:
            continue
        start_value = _coerce_positive_time(entry.get("start"))
        end_value = _coerce_positive_time(entry.get("end"))
        title_value: Optional[str] = None
        for key in ("title", "display_title", "spoken_title", "original_title"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                title_value = value.strip()
                break
        timing_map[index_value] = {
            "start": start_value,
            "end": end_value,
            "title": title_value,
        }

    chapter_timings: List[Dict[str, Any]] = []
    for idx, chapter in enumerate(chapters):
        marker = timing_map.get(idx)
        if marker and marker.get("title") and isinstance(chapter, dict):
            chapter_title = marker["title"]
            if isinstance(chapter_title, str) and chapter_title.strip():
                chapter["title"] = chapter_title
        chapter_timings.append(
            {
                "index": idx,
                "start": marker.get("start") if marker else None,
                "end": marker.get("end") if marker else None,
                "title": marker.get("title") if marker else None,
            }
        )

    return render_template(
        "reader_embed.html",
        job=job,
        audio_url=audio_url,
        epub_url=epub_url,
        chapters=chapters,
        chapter_url=chapter_url,
        asset_base=asset_base,
        chapter_timings=chapter_timings,
        display_title=display_title,
    )


@web_bp.get("/jobs/<job_id>/reader/chapter")
def job_reader_chapter(job_id: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    epub_path = _locate_job_epub(job)
    if not epub_path:
        abort(404)
    raw_href = request.args.get("href", "").strip()
    if not raw_href:
        abort(400)
    try:
        chapter_bytes = _read_epub_bytes(epub_path, raw_href)
    except (ValueError, FileNotFoundError, KeyError):
        abort(404)
    content = _decode_text(chapter_bytes)
    return jsonify({"content": content})


@web_bp.get("/jobs/<job_id>/reader/asset/<path:asset_path>")
def job_reader_asset(job_id: str, asset_path: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    epub_path = _locate_job_epub(job)
    if not epub_path:
        abort(404)
    try:
        payload = _read_epub_bytes(epub_path, asset_path)
    except (ValueError, FileNotFoundError, KeyError):
        abort(404)
    mime_type, _ = mimetypes.guess_type(asset_path)
    buffer = io.BytesIO(payload)
    buffer.seek(0)
    return send_file(
        buffer,
        mimetype=mime_type or "application/octet-stream",
        as_attachment=False,
        download_name=posixpath.basename(asset_path) or "asset",
    )


@web_bp.get("/jobs/<job_id>/download")
def download_job(job_id: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    audio_path = _locate_job_audio(job)
    if not audio_path:
        abort(404)
    mime_type, _ = mimetypes.guess_type(str(audio_path))
    return send_file(
        audio_path,
        mimetype=mime_type or "application/octet-stream",
        as_attachment=True,
        download_name=audio_path.name,
    )


@web_bp.get("/jobs/<job_id>/download/m4b")
def download_job_m4b(job_id: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    audio_path = _locate_job_m4b(job)
    if not audio_path:
        abort(404)
    mime_type, _ = mimetypes.guess_type(str(audio_path))
    return send_file(
        audio_path,
        mimetype=mime_type or "audio/mpeg",
        as_attachment=True,
        download_name=audio_path.name,
    )


@web_bp.get("/jobs/<job_id>/download/epub3")
def download_job_epub3(job_id: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    epub_path = _locate_job_epub(job)
    if not epub_path:
        abort(404)
    return send_file(
        epub_path,
        mimetype="application/epub+zip",
        as_attachment=True,
        download_name=epub_path.name,
        conditional=True,
    )


@web_bp.get("/partials/jobs")
def jobs_partial() -> str:
    return _render_jobs_panel()

@web_bp.get("/partials/jobs/<job_id>/logs")
def job_logs_partial(job_id: str) -> str:
    job = _service().get_job(job_id)
    if not job:
        abort(404)
    return render_template("partials/logs.html", job=job, static_view=False)


@web_bp.get("/jobs/<job_id>/logs/static")
def job_logs_static(job_id: str) -> str:
    job = _service().get_job(job_id)
    if not job:
        abort(404)
    log_lines = [
        f"{datetime.fromtimestamp(entry.timestamp).strftime('%Y-%m-%d %H:%M:%S')} [{entry.level.upper()}] {entry.message}"
        for entry in job.logs
    ]
    return render_template(
        "job_logs_static.html",
        job=job,
        log_text="\n".join(log_lines),
        static_view=True,
    )


@api_bp.get("/jobs/<job_id>")
def job_json(job_id: str) -> ResponseReturnValue:
    job = _service().get_job(job_id)
    if job is None:
        abort(404)
    if not isinstance(job, Job):  # pragma: no cover - defensive guard
        abort(404)
    job_obj = cast(Job, job)
    payload = job_obj.as_dict()
    return jsonify(payload)
