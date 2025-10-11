from __future__ import annotations

import io
import json
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
from werkzeug.utils import secure_filename

import numpy as np
import soundfile as sf
from abogen.constants import (
    LANGUAGE_DESCRIPTIONS,
    SAMPLE_VOICE_TEXTS,
    SUBTITLE_FORMATS,
    SUPPORTED_LANGUAGES_FOR_SUBTITLE_GENERATION,
    SUPPORTED_SOUND_FORMATS,
    VOICES_INTERNAL,
)
from abogen.chunking import ChunkLevel, build_chunks_for_chapters
from abogen.kokoro_text_normalization import normalize_roman_numeral_titles
from abogen.utils import (
    calculate_text_length,
    clean_text,
    get_user_output_path,
    load_config,
    load_numpy_kpipeline,
    save_config,
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

_SPEAKER_MODE_OPTIONS = [
    {"value": "single", "label": "Single Speaker"},
    {"value": "multi", "label": "Multi-Speaker"},
]

_CHUNK_LEVEL_VALUES = {option["value"] for option in _CHUNK_LEVEL_OPTIONS}
_SPEAKER_MODE_VALUES = {option["value"] for option in _SPEAKER_MODE_OPTIONS}


_DEFAULT_ANALYSIS_THRESHOLD = 3


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
    base = base_dir.strip("/")
    combined = posixpath.join(base, sanitized) if base else sanitized
    normalized = posixpath.normpath(combined)
    if normalized in {"", "."}:
        return ""
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
                normalized = _normalize_epub_path("", href)
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
                    normalized = _normalize_epub_path("", href)
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
    speaker_mode: str,
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
    analysis_enabled = speaker_mode == "multi" and run_analysis
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
            ((sid, meta) for sid, meta in speakers_payload.items() if sid != "narrator"),
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

    raw_speaker_mode = (form.get("speaker_mode") or pending.speaker_mode or "single").strip().lower()
    if raw_speaker_mode not in _SPEAKER_MODE_VALUES:
        raw_speaker_mode = "single"
    pending.speaker_mode = raw_speaker_mode

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
        "speaker_modes": _SPEAKER_MODE_OPTIONS,
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

BOOLEAN_SETTINGS = {
    "replace_single_newlines",
    "use_gpu",
    "save_chapters_separately",
    "merge_chapters_at_end",
    "save_as_project",
    "generate_epub3",
}

FLOAT_SETTINGS = {"silence_between_chapters", "chapter_intro_delay"}
INT_SETTINGS = {"max_subtitle_words", "speaker_analysis_threshold"}


def _has_output_override() -> bool:
    return bool(os.environ.get("ABOGEN_OUTPUT_DIR") or os.environ.get("ABOGEN_OUTPUT_ROOT"))


def _settings_defaults() -> Dict[str, Any]:
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
        "speaker_mode": "single",
        "generate_epub3": False,
        "speaker_analysis_threshold": _DEFAULT_ANALYSIS_THRESHOLD,
        "speaker_pronunciation_sentence": "This is {{name}} speaking.",
        "speaker_random_languages": [],
    }


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
    if key == "speaker_mode":
        if isinstance(value, str) and value in _SPEAKER_MODE_VALUES:
            return value
        return defaults[key]
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
        updated["speaker_mode"] = _normalize_setting_value(
            "speaker_mode", form.get("speaker_mode"), defaults
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

        cfg = load_config() or {}
        cfg.update(updated)
        save_config(cfg)
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
    }
    return render_template("settings.html", **context)


@web_bp.get("/voices")
def voice_profiles_page() -> str:
    options = _template_options()
    return render_template("voices.html", options=options)


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


@web_bp.post("/jobs")
def enqueue_job() -> ResponseReturnValue:
    service = _service()
    uploads_dir = Path(current_app.config["UPLOAD_FOLDER"])
    uploads_dir.mkdir(parents=True, exist_ok=True)

    file = request.files.get("source_file")
    text_input = request.form.get("source_text", "").strip()

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

    profiles = load_profiles()
    settings = _load_settings()

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

    chunk_level_default = str(settings.get("chunk_level", "paragraph")).strip().lower()
    raw_chunk_level = (request.form.get("chunk_level") or chunk_level_default).strip().lower()
    if raw_chunk_level not in _CHUNK_LEVEL_VALUES:
        raw_chunk_level = chunk_level_default if chunk_level_default in _CHUNK_LEVEL_VALUES else "paragraph"
    chunk_level_value = raw_chunk_level
    chunk_level_literal = cast(ChunkLevel, chunk_level_value)

    speaker_mode_default = str(settings.get("speaker_mode", "single")).strip().lower()
    raw_speaker_mode = (request.form.get("speaker_mode") or speaker_mode_default).strip().lower()
    if raw_speaker_mode not in _SPEAKER_MODE_VALUES:
        raw_speaker_mode = "single"
    speaker_mode_value = raw_speaker_mode

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

    initial_analysis = speaker_mode_value == "multi"
    processed_chunks, speakers, analysis_payload, config_languages, _ = _prepare_speaker_metadata(
        chapters=selected_chapter_sources,
        chunks=raw_chunks,
        analysis_chunks=analysis_chunks,
        speaker_mode=speaker_mode_value,
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
        chunk_level=chunk_level_value,
        speaker_mode=speaker_mode_value,
        generate_epub3=generate_epub3,
        chunks=processed_chunks,
        speakers=speakers,
        speaker_analysis=analysis_payload,
        speaker_analysis_threshold=analysis_threshold,
        analysis_requested=initial_analysis,
    )

    service.store_pending_job(pending)
    pending.applied_speaker_config = selected_speaker_config or None
    if config_languages:
        pending.speaker_voice_languages = list(config_languages)
    elif isinstance(speaker_config_payload, Mapping):
        languages = speaker_config_payload.get("languages")
        if isinstance(languages, list):
            pending.speaker_voice_languages = [code for code in languages if isinstance(code, str)]
    return redirect(url_for("web.prepare_job", pending_id=pending.id))


@web_bp.get("/jobs/prepare/<pending_id>")
def prepare_job(pending_id: str) -> str:
    pending = _service().get_pending_job(pending_id)
    if not pending:
        abort(404)
    pending = cast(PendingJob, pending)
    return _render_prepare_page(pending, active_step="chapters")


@web_bp.post("/jobs/prepare/<pending_id>/analyze")
def analyze_pending_job(pending_id: str) -> ResponseReturnValue:
    service = _service()
    pending = service.get_pending_job(pending_id)
    if not pending:
        abort(404)
    pending = cast(PendingJob, pending)

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
        return _render_prepare_page(pending, error=" ".join(errors), active_step="chapters")

    if pending.speaker_mode != "multi":
        setattr(pending, "analysis_requested", False)
        pending.chunks = []
        pending.speaker_analysis = {}
        return _render_prepare_page(
            pending,
            error="Switch to multi-speaker mode to analyze speakers.",
            active_step="chapters",
        )

    if not enabled_overrides:
        setattr(pending, "analysis_requested", False)
        pending.chunks = []
        pending.speaker_analysis = {}
        return _render_prepare_page(
            pending,
            error="Select at least one chapter to analyze.",
            active_step="chapters",
        )

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
        speaker_mode=pending.speaker_mode,
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

    service.store_pending_job(pending)

    notice_message = "Speaker analysis updated."
    if persist_config_requested and config_name:
        notice_message = "Speaker analysis updated and configuration saved."
    return _render_prepare_page(pending, notice=notice_message, active_step="speakers")


@web_bp.post("/jobs/prepare/<pending_id>")
def finalize_job(pending_id: str) -> ResponseReturnValue:
    service = _service()
    pending = service.get_pending_job(pending_id)
    if not pending:
        abort(404)
    pending = cast(PendingJob, pending)

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
        return _render_prepare_page(
            pending,
            error=" ".join(errors),
            active_step=request.form.get("active_step") or "speakers",
        )

    if pending.speaker_mode != "multi":
        setattr(pending, "analysis_requested", False)

    if not enabled_overrides:
        pending.chunks = []
        return _render_prepare_page(
            pending,
            error="Select at least one chapter to convert.",
            active_step="chapters",
        )

    active_step = (request.form.get("active_step") or "chapters").strip().lower()

    raw_chunks = build_chunks_for_chapters(enabled_overrides, level=chunk_level_literal)
    analysis_chunks = build_chunks_for_chapters(enabled_overrides, level="sentence")
    is_multi = pending.speaker_mode == "multi"
    analysis_requested = bool(getattr(pending, "analysis_requested", False))
    should_force_speakers = is_multi and active_step != "speakers"

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
    run_analysis = is_multi and (should_force_speakers or analysis_requested)
    processed_chunks, roster, analysis_payload, config_languages, updated_config = _prepare_speaker_metadata(
        chapters=enabled_overrides,
        chunks=raw_chunks,
        analysis_chunks=analysis_chunks,
        speaker_mode=pending.speaker_mode,
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

    if should_force_speakers:
        notice_message = "Review speaker assignments before queuing."
        if persist_config_requested and config_key:
            notice_message = "Configuration saved. Review speaker assignments before queuing."
        service.store_pending_job(pending)
        return _render_prepare_page(
            pending,
            notice=notice_message,
            active_step="speakers",
        )

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
        chunk_level=pending.chunk_level,
        chunks=processed_chunks,
        speakers=roster,
        speaker_mode=pending.speaker_mode,
        generate_epub3=pending.generate_epub3,
        speaker_analysis=pending.speaker_analysis,
        speaker_analysis_threshold=pending.speaker_analysis_threshold,
        analysis_requested=getattr(pending, "analysis_requested", False),
    )

    if config_languages:
        job.speaker_voice_languages = list(config_languages)
    elif pending.speaker_voice_languages:
        job.speaker_voice_languages = list(pending.speaker_voice_languages)

    if isinstance(config_key, str) and config_key:
        job.applied_speaker_config = config_key

    return redirect(url_for("web.index", _anchor="queue"))


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
    return redirect(url_for("web.index", _anchor="queue"))


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


def _render_prepare_page(
    pending: PendingJob,
    *,
    error: Optional[str] = None,
    notice: Optional[str] = None,
    active_step: Optional[str] = None,
) -> str:
    if not active_step:
        active_step = (
            request.form.get("active_step")
            if request.method == "POST"
            else request.args.get("step")
        ) or "chapters"

    normalized_step = (active_step or "chapters").strip().lower()
    if normalized_step not in {"chapters", "speakers"}:
        normalized_step = "chapters"

    is_multi = pending.speaker_mode == "multi"
    if normalized_step == "speakers" and not is_multi:
        normalized_step = "chapters"

    template_name = "prepare_speakers.html" if normalized_step == "speakers" else "prepare_chapters.html"

    return render_template(
        template_name,
        pending=pending,
        options=_template_options(),
        settings=_load_settings(),
        error=error,
        notice=notice,
        active_step=normalized_step,
    )


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
    return render_template(
        "reader_embed.html",
        job=job,
        audio_url=audio_url,
        epub_url=epub_url,
        chapters=chapters,
        chapter_url=chapter_url,
        asset_base=asset_base,
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
