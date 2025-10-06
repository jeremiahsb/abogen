from __future__ import annotations

import io
import json
import mimetypes
import threading
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
from abogen.utils import calculate_text_length, clean_text, load_config, load_numpy_kpipeline
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
from .conversion_runner import SPLIT_PATTERN, SAMPLE_RATE, _select_device, _to_float32
from .service import ConversionService, Job, JobStatus

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
    return {
        "languages": LANGUAGE_DESCRIPTIONS,
        "voices": VOICES_INTERNAL,
        "subtitle_formats": SUBTITLE_FORMATS,
        "supported_langs_for_subs": SUPPORTED_LANGUAGES_FOR_SUBTITLE_GENERATION,
        "output_formats": SUPPORTED_SOUND_FORMATS,
        "voice_profiles": ordered_profiles,
        "separate_formats": ["wav", "flac", "mp3", "opus"],
        "voice_catalog": _build_voice_catalog(),
        "sample_voice_texts": SAMPLE_VOICE_TEXTS,
        "voice_profiles_data": profiles,
    }


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
    service = _service()
    jobs = service.list_jobs()
    return render_template(
        "index.html",
        jobs=jobs,
        options=_template_options(),
    )


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
    max_seconds = float(payload.get("max_seconds", 12.0) or 12.0)
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

    cfg = load_config()
    use_gpu_cfg = bool(cfg.get("use_gpu", True))
    use_gpu = use_gpu_cfg if payload.get("use_gpu") is None else bool(payload.get("use_gpu"))
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
        total_chars = 0
    else:
        original_name = "direct_text.txt"
        stored_path = uploads_dir / f"{uuid.uuid4().hex}_{original_name}"
        stored_path.write_text(text_input, encoding="utf-8")
        total_chars = calculate_text_length(clean_text(text_input))

    profiles = load_profiles()

    language = request.form.get("language", "a")
    base_voice = request.form.get("voice", "af_alloy")
    profile_name = request.form.get("voice_profile", "").strip()
    custom_formula = request.form.get("voice_formula", "").strip()
    voice, language, selected_profile = _resolve_voice_choice(
        language,
        base_voice,
        profile_name,
        custom_formula,
        profiles,
    )
    speed = float(request.form.get("speed", "1.0"))
    subtitle_mode = request.form.get("subtitle_mode", "Disabled")
    output_format = request.form.get("output_format", "wav")
    subtitle_format = request.form.get("subtitle_format", "srt")
    save_mode = request.form.get("save_mode", "Save next to input file")
    replace_single_newlines = request.form.get("replace_single_newlines") in {"true", "on", "1"}
    use_gpu = request.form.get("use_gpu") in {"true", "on", "1"}
    save_chapters_separately = request.form.get("save_chapters_separately") in {"true", "on", "1"}
    merge_chapters_at_end = request.form.get("merge_chapters_at_end") in {"true", "on", "1"}
    if not save_chapters_separately:
        merge_chapters_at_end = True
    save_as_project = request.form.get("save_as_project") in {"true", "on", "1"}
    separate_chapters_format = request.form.get("separate_chapters_format", "wav").lower()
    try:
        silence_between_chapters = float(request.form.get("silence_between_chapters", "2.0") or 0.0)
    except ValueError:
        silence_between_chapters = 2.0
    silence_between_chapters = max(0.0, silence_between_chapters)
    try:
        max_subtitle_words = int(request.form.get("max_subtitle_words", "50") or 50)
    except ValueError:
        max_subtitle_words = 50
    max_subtitle_words = max(1, min(max_subtitle_words, 200))

    job = service.enqueue(
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
    )
    return redirect(url_for("web.job_detail", job_id=job.id))


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


@web_bp.post("/jobs/<job_id>/cancel")
def cancel_job(job_id: str) -> Response:
    _service().cancel(job_id)
    return redirect(url_for("web.job_detail", job_id=job_id))


@web_bp.post("/jobs/<job_id>/delete")
def delete_job(job_id: str) -> Response:
    _service().delete(job_id)
    return redirect(url_for("web.index"))


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
    return render_template("partials/jobs.html", jobs=_service().list_jobs())


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
