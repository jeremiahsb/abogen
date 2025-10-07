from __future__ import annotations

import io
import json
import mimetypes
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, cast

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

from abogen.voice_formulas import get_new_voice
from abogen.text_extractor import extract_from_path
from .conversion_runner import SPLIT_PATTERN, SAMPLE_RATE, _select_device, _to_float32
from .service import ConversionService, Job, JobStatus, PendingJob

web_bp = Blueprint("web", __name__)
api_bp = Blueprint("api", __name__)


_preview_pipeline_lock = threading.RLock()
_preview_pipelines: Dict[Tuple[str, str], Any] = {}


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
                "display_name": rest.replace("_", " ").title() if rest else voice_id,
            }
        )
    return catalog


def _template_options() -> Dict[str, Any]:
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
    return {
        "languages": LANGUAGE_DESCRIPTIONS,
        "voices": VOICES_INTERNAL,
        "subtitle_formats": SUBTITLE_FORMATS,
        "supported_langs_for_subs": SUPPORTED_LANGUAGES_FOR_SUBTITLE_GENERATION,
        "output_formats": SUPPORTED_SOUND_FORMATS,
        "voice_profiles": ordered_profiles,
        "voice_profile_options": profile_options,
        "separate_formats": ["wav", "flac", "mp3", "opus"],
        "voice_catalog": _build_voice_catalog(),
        "sample_voice_texts": SAMPLE_VOICE_TEXTS,
        "voice_profiles_data": profiles,
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
}

FLOAT_SETTINGS = {"silence_between_chapters", "chapter_intro_delay"}
INT_SETTINGS = {"max_subtitle_words"}


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
    parts = [segment.strip() for segment in formula.split("+") if segment.strip()]
    voices: List[tuple[str, float]] = []
    for part in parts:
        if "*" not in part:
            raise ValueError("Each component must be in the form voice*weight")
        name, weight_str = part.split("*", 1)
        name = name.strip()
        if name not in VOICES_INTERNAL:
            raise ValueError(f"Unknown voice '{name}'")
        try:
            weight = float(weight_str.strip())
        except ValueError as exc:  # pragma: no cover - validated via form
            raise ValueError(f"Invalid weight for {name}") from exc
        if weight <= 0:
            raise ValueError(f"Weight for {name} must be positive")
        voices.append((name, weight))
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
def queue_page() -> str:
    return render_template("queue.html", jobs_panel=_render_jobs_panel())


@web_bp.route("/settings", methods=["GET", "POST"])
def settings_page() -> Response | str:
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


@web_bp.post("/voices")
def save_voice_profile_route() -> Response:
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
def delete_voice_profile_route(name: str) -> Response:
    delete_profile(name)
    return redirect(url_for("web.voice_profiles_page"))


@api_bp.get("/voice-profiles")
def api_list_voice_profiles() -> Response:
    return jsonify(_profiles_payload())


@api_bp.post("/voice-profiles")
def api_save_voice_profile() -> Response:
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
def api_delete_voice_profile(name: str) -> Response:
    remove_profile(name)
    return jsonify({"ok": True, **_profiles_payload()})


@api_bp.post("/voice-profiles/<name>/duplicate")
def api_duplicate_voice_profile(name: str) -> Response:
    payload = request.get_json(silent=True) or {}
    new_name = (payload.get("name") or payload.get("new_name") or "").strip()
    if not new_name:
        abort(400, "Duplicate name is required")
    duplicate_profile(name, new_name)
    return jsonify({"ok": True, "profile": new_name, **_profiles_payload()})


@api_bp.post("/voice-profiles/import")
def api_import_voice_profiles() -> Response:
    replace = False
    data: Optional[Dict[str, Any]] = None
    if "file" in request.files:
        file_storage = request.files["file"]
        try:
            data = json.load(file_storage)
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
def api_export_voice_profiles() -> Response:
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
def api_preview_voice_mix() -> Response:
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


@web_bp.post("/jobs")
def enqueue_job() -> Response:
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

    metadata_tags = extraction.metadata or {}
    total_chars = extraction.total_characters or calculate_text_length(extraction.combined_text)
    chapters_payload: List[Dict[str, Any]] = []
    for index, chapter in enumerate(extraction.chapters):
        chapters_payload.append(
            {
                "id": f"{index:04d}",
                "index": index,
                "title": chapter.title,
                "text": chapter.text,
                "characters": len(chapter.text),
                "enabled": True,
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

    profiles = load_profiles()
    settings = _load_settings()

    language = request.form.get("language", "a")
    base_voice = request.form.get("voice", "af_alloy")
    profile_selection = (request.form.get("voice_profile") or "__standard").strip()
    custom_formula_raw = request.form.get("voice_formula", "").strip()

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
        voice_profile=selected_profile,
        max_subtitle_words=max_subtitle_words,
        metadata_tags=metadata_tags,
        chapters=chapters_payload,
        created_at=time.time(),
        cover_image_path=cover_path,
        cover_image_mime=cover_mime,
        chapter_intro_delay=chapter_intro_delay,
    )

    service.store_pending_job(pending)
    return redirect(url_for("web.prepare_job", pending_id=pending.id))


@web_bp.get("/jobs/prepare/<pending_id>")
def prepare_job(pending_id: str) -> str:
    pending = _service().get_pending_job(pending_id)
    if not pending:
        abort(404)
    pending = cast(PendingJob, pending)
    return _render_prepare_page(pending)


@web_bp.post("/jobs/prepare/<pending_id>")
def finalize_job(pending_id: str) -> Response:
    service = _service()
    pending = service.get_pending_job(pending_id)
    if not pending:
        abort(404)
    pending = cast(PendingJob, pending)

    profiles = serialize_profiles()
    delay_value = pending.chapter_intro_delay
    raw_delay = request.form.get("chapter_intro_delay")
    if raw_delay is not None:
        raw_normalized = raw_delay.strip()
        if raw_normalized:
            try:
                delay_value = max(0.0, float(raw_normalized))
            except ValueError:
                return _render_prepare_page(pending, error="Enter a valid number for the chapter intro delay.")
        else:
            delay_value = 0.0
    pending.chapter_intro_delay = delay_value

    overrides: List[Dict[str, Any]] = []
    selected_total = 0
    errors: List[str] = []

    for index, chapter in enumerate(pending.chapters):
        enabled = request.form.get(f"chapter-{index}-enabled") == "on"
        title_input = (request.form.get(f"chapter-{index}-title") or "").strip()
        title = title_input or chapter.get("title") or f"Chapter {index + 1}"
        voice_selection = request.form.get(f"chapter-{index}-voice", "__default")
        formula_input = (request.form.get(f"chapter-{index}-formula") or "").strip()

        entry: Dict[str, Any] = {
            "id": chapter.get("id") or f"{index:04d}",
            "index": index,
            "order": index,
            "source_title": chapter.get("title") or title,
            "title": title,
            "text": chapter.get("text", ""),
            "enabled": enabled,
        }
        entry["characters"] = len(entry["text"])

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
            selected_total += len(entry["text"] or "")

        overrides.append(entry)
        pending.chapters[index] = dict(entry)

    if not any(item.get("enabled") for item in overrides):
        return _render_prepare_page(pending, error="Select at least one chapter to convert.")

    if errors:
        return _render_prepare_page(pending, error=" ".join(errors))

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
        metadata_tags=pending.metadata_tags,
        save_chapters_separately=pending.save_chapters_separately,
        merge_chapters_at_end=pending.merge_chapters_at_end,
        separate_chapters_format=pending.separate_chapters_format,
        silence_between_chapters=pending.silence_between_chapters,
        save_as_project=pending.save_as_project,
        voice_profile=pending.voice_profile,
        max_subtitle_words=pending.max_subtitle_words,
        cover_image_path=pending.cover_image_path,
        cover_image_mime=pending.cover_image_mime,
        chapter_intro_delay=pending.chapter_intro_delay,
    )

    return redirect(url_for("web.job_detail", job_id=job.id))


@web_bp.post("/jobs/prepare/<pending_id>/cancel")
def cancel_pending_job(pending_id: str) -> Response:
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
    return redirect(url_for("web.index"))


def _render_jobs_panel() -> str:
    jobs = _service().list_jobs()
    active_statuses = {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED}
    active_jobs = [job for job in jobs if job.status in active_statuses]
    active_jobs.sort(key=lambda job: ((job.queue_position or 10_000), -job.created_at))
    finished_jobs = [job for job in jobs if job.status not in active_statuses]
    return render_template(
        "partials/jobs.html",
        active_jobs=active_jobs,
        finished_jobs=finished_jobs[:5],
        total_finished=len(finished_jobs),
        JobStatus=JobStatus,
    )


def _render_prepare_page(pending: PendingJob, *, error: Optional[str] = None) -> str:
    return render_template(
        "prepare_job.html",
        pending=pending,
        options=_template_options(),
        settings=_load_settings(),
        error=error,
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
    )


@web_bp.post("/jobs/<job_id>/pause")
def pause_job(job_id: str) -> Response:
    _service().pause(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/resume")
def resume_job(job_id: str) -> Response:
    _service().resume(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str) -> Response:
    _service().cancel(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/delete")
def delete_job(job_id: str) -> Response:
    _service().delete(job_id)
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.index"))


@web_bp.post("/jobs/clear-finished")
def clear_finished_jobs() -> Response:
    _service().clear_finished()
    if request.headers.get("HX-Request"):
        return _render_jobs_panel()
    return redirect(url_for("web.queue_page"))


@web_bp.get("/jobs/<job_id>/download")
def download_job(job_id: str) -> Response:
    job = _service().get_job(job_id)
    if job is None or job.status != JobStatus.COMPLETED:
        abort(404)
    result = getattr(job, "result", None)
    audio_path = getattr(result, "audio_path", None)
    if audio_path is None:
        abort(404)
    if not isinstance(audio_path, Path):  # pragma: no cover - sanity guard
        abort(404)
    audio_path_path = cast(Path, audio_path)
    if not audio_path_path.exists():
        abort(404)
    mime_type, _ = mimetypes.guess_type(str(audio_path_path))
    return send_file(
        audio_path_path,
        mimetype=mime_type or "application/octet-stream",
        as_attachment=True,
        download_name=audio_path_path.name,
    )


@web_bp.get("/partials/jobs")
def jobs_partial() -> str:
    return _render_jobs_panel()

@web_bp.get("/partials/jobs/<job_id>/logs")
def job_logs_partial(job_id: str) -> str:
    job = _service().get_job(job_id)
    if not job:
        abort(404)
    return render_template("partials/logs.html", job=job)


@api_bp.get("/jobs/<job_id>")
def job_json(job_id: str) -> Response:
    job = _service().get_job(job_id)
    if job is None:
        abort(404)
    if not isinstance(job, Job):  # pragma: no cover - defensive guard
        abort(404)
    job_obj = cast(Job, job)
    payload = job_obj.as_dict()
    return jsonify(payload)
