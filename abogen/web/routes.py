from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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

from abogen.constants import (
    LANGUAGE_DESCRIPTIONS,
    SUBTITLE_FORMATS,
    SUPPORTED_LANGUAGES_FOR_SUBTITLE_GENERATION,
    SUPPORTED_SOUND_FORMATS,
    VOICES_INTERNAL,
)
from abogen.utils import calculate_text_length, clean_text
from abogen.voice_profiles import delete_profile, load_profiles, save_profiles

from .service import ConversionService, Job, JobStatus

web_bp = Blueprint("web", __name__)
api_bp = Blueprint("api", __name__)


def _service() -> ConversionService:
    return current_app.extensions["conversion_service"]


def _template_options() -> Dict[str, Any]:
    profiles = load_profiles()
    ordered_profiles = sorted(profiles.items())
    return {
        "languages": LANGUAGE_DESCRIPTIONS,
        "voices": VOICES_INTERNAL,
        "subtitle_formats": SUBTITLE_FORMATS,
        "supported_langs_for_subs": SUPPORTED_LANGUAGES_FOR_SUBTITLE_GENERATION,
        "output_formats": SUPPORTED_SOUND_FORMATS,
        "voice_profiles": ordered_profiles,
        "separate_formats": ["wav", "flac", "mp3", "opus"],
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
    profiles = load_profiles()
    rendered = []
    for name, data in sorted(profiles.items()):
        rendered.append(
            {
                "name": name,
                "language": data.get("language", "a"),
                "formula": _formula_from_profile(data) or "",
            }
        )
    return render_template(
        "voices.html",
        profiles=rendered,
        languages=LANGUAGE_DESCRIPTIONS,
        voices=VOICES_INTERNAL,
    )


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
    if not job or job.status != JobStatus.COMPLETED:
        abort(404)
    if not job.result.audio_path:
        abort(404)
    path = job.result.audio_path
    if not path.exists():
        abort(404)
    mime_type, _ = mimetypes.guess_type(str(path))
    return send_file(
        path,
        mimetype=mime_type or "application/octet-stream",
        as_attachment=True,
        download_name=path.name,
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
    if not job:
        abort(404)
    return jsonify(job.as_dict())
