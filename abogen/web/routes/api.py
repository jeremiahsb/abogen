from typing import Any, Dict, Mapping, List, Optional
import base64
import uuid
from pathlib import Path

from flask import Blueprint, request, jsonify, send_file, url_for, current_app
from flask.typing import ResponseReturnValue

from abogen.web.routes.utils.settings import (
    load_settings,
    load_integration_settings,
    coerce_float,
    coerce_bool,
)
from abogen.voice_profiles import (
    load_profiles,
    save_profiles,
    delete_profile,
    serialize_profiles,
)
from abogen.web.routes.utils.preview import synthesize_preview, generate_preview_audio
from abogen.normalization_settings import (
    build_llm_configuration,
    build_apostrophe_config,
    apply_overrides,
)
from abogen.llm_client import list_models, LLMClientError
from abogen.kokoro_text_normalization import normalize_for_pipeline
from abogen.integrations.audiobookshelf import AudiobookshelfClient, AudiobookshelfConfig
from abogen.integrations.calibre_opds import (
    CalibreOPDSClient,
    CalibreOPDSError,
)
from abogen.web.routes.utils.service import get_service
from abogen.web.routes.utils.form import build_pending_job_from_extraction
from abogen.text_extractor import extract_from_path
from werkzeug.utils import secure_filename

api_bp = Blueprint("api", __name__)

# --- Voice Profile Routes ---

@api_bp.get("/voice-profiles")
def api_get_voice_profiles() -> ResponseReturnValue:
    profiles = load_profiles()
    return jsonify(profiles)

@api_bp.post("/voice-profiles")
def api_save_voice_profile() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=True) or {}
    name = payload.get("name")
    profile = payload.get("profile")
    
    if not name or not profile:
        return jsonify({"error": "Name and profile are required"}), 400
        
    profiles = load_profiles()
    profiles[name] = profile
    save_profiles(profiles)
    return jsonify({"success": True})

@api_bp.delete("/voice-profiles/<path:name>")
def api_delete_voice_profile(name: str) -> ResponseReturnValue:
    delete_profile(name)
    return jsonify({"success": True})

@api_bp.post("/speaker-preview")
def api_speaker_preview() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=True) or {}
    text = payload.get("text", "Hello world")
    voice = payload.get("voice", "af_heart")
    language = payload.get("language", "a")
    speed = coerce_float(payload.get("speed"), 1.0)
    
    settings = load_settings()
    use_gpu = settings.get("use_gpu", False)
    
    try:
        return synthesize_preview(
            text=text,
            voice_spec=voice,
            language=language,
            speed=speed,
            use_gpu=use_gpu
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Integration Routes ---

@api_bp.get("/integrations/calibre-opds/feed")
def api_calibre_opds_feed() -> ResponseReturnValue:
    integrations = load_integration_settings()
    calibre_settings = integrations.get("calibre_opds", {})
    
    payload = {
        "base_url": calibre_settings.get("base_url"),
        "username": calibre_settings.get("username"),
        "password": calibre_settings.get("password"),
        "verify_ssl": calibre_settings.get("verify_ssl", True),
    }
    
    if not payload.get("base_url"):
        return jsonify({"error": "Calibre OPDS base URL is not configured."}), 400
        
    try:
        client = CalibreOPDSClient(
            base_url=payload.get("base_url") or "",
            username=payload.get("username"),
            password=payload.get("password"),
            verify=bool(payload.get("verify_ssl", True)),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    href = request.args.get("href", type=str)
    query = request.args.get("q", type=str)
    letter = request.args.get("letter", type=str)
    
    try:
        if letter:
            feed = client.browse_letter(letter, start_href=href)
        elif query:
            feed = client.search(query, start_href=href)
        else:
            feed = client.fetch_feed(href)
    except CalibreOPDSError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        return jsonify({"error": f"Unexpected error: {str(exc)}"}), 500

    return jsonify({
        "feed": feed.to_dict(),
        "href": href or "",
        "query": query or "",
    })

@api_bp.post("/integrations/audiobookshelf/folders")
def api_abs_folders() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=True) or {}
    host = payload.get("base_url") or payload.get("host")
    token = payload.get("api_token") or payload.get("token")
    library_id = payload.get("library_id")
    
    if not host or not token:
        return jsonify({"error": "Base URL and API token are required"}), 400
    
    if not library_id:
        return jsonify({"error": "Library ID is required to list folders"}), 400
        
    try:
        config = AudiobookshelfConfig(base_url=host, api_token=token, library_id=library_id)
        client = AudiobookshelfClient(config)
        folders = client.list_folders()
        return jsonify({"folders": folders})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@api_bp.post("/integrations/audiobookshelf/test")
def api_abs_test() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=True) or {}
    host = payload.get("base_url") or payload.get("host")
    token = payload.get("api_token") or payload.get("token")
    
    if not host or not token:
        return jsonify({"error": "Base URL and API token are required"}), 400
        
    try:
        config = AudiobookshelfConfig(base_url=host, api_token=token)
        client = AudiobookshelfClient(config)
        # Just getting libraries is a good enough test
        client.get_libraries()
        return jsonify({"success": True, "message": "Connection successful."})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@api_bp.post("/integrations/calibre-opds/test")
def api_calibre_opds_test() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=True) or {}
    base_url = payload.get("base_url")
    username = payload.get("username")
    password = payload.get("password")
    verify_ssl = coerce_bool(payload.get("verify_ssl"), False)
    
    if not base_url:
        return jsonify({"error": "Base URL is required"}), 400
        
    try:
        client = CalibreOPDSClient(
            base_url=base_url,
            username=username,
            password=password,
            verify=verify_ssl,
            timeout=10.0
        )
        client.fetch_feed()
        return jsonify({"success": True, "message": "Connection successful."})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@api_bp.post("/integrations/calibre-opds/import")
def api_calibre_opds_import() -> ResponseReturnValue:
    if not request.is_json:
        return jsonify({"error": "Expected JSON payload."}), 400
    
    data = request.get_json(force=True, silent=True) or {}
    href = str(data.get("href") or "").strip()
    
    if not href:
        return jsonify({"error": "Download URL (href) is required."}), 400
        
    metadata_payload = data.get("metadata") if isinstance(data, Mapping) else None
    metadata_overrides: Dict[str, Any] = {}
    
    if isinstance(metadata_payload, Mapping):
        def _stringify_metadata_value(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, (list, tuple, set)):
                parts = [str(item).strip() for item in value if item is not None]
                parts = [part for part in parts if part]
                return ", ".join(parts)
            text = str(value).strip()
            return text

        raw_series = metadata_payload.get("series") or metadata_payload.get("series_name")
        series_name = str(raw_series or "").strip()
        
        if series_name:
            metadata_overrides["series"] = series_name
            metadata_overrides.setdefault("series_name", series_name)
            
        series_index_value = (
            metadata_payload.get("series_index")
            or metadata_payload.get("series_position")
            or metadata_payload.get("series_sequence")
            or metadata_payload.get("book_number")
        )
        if series_index_value is not None:
            series_index_text = str(series_index_value).strip()
            if series_index_text:
                metadata_overrides.setdefault("series_index", series_index_text)
                metadata_overrides.setdefault("series_position", series_index_text)
                metadata_overrides.setdefault("series_sequence", series_index_text)
                metadata_overrides.setdefault("book_number", series_index_text)
                
        tags_value = metadata_payload.get("tags") or metadata_payload.get("keywords")
        if tags_value:
            tags_text = _stringify_metadata_value(tags_value)
            if tags_text:
                metadata_overrides.setdefault("tags", tags_text)
                metadata_overrides.setdefault("keywords", tags_text)
                metadata_overrides.setdefault("genre", tags_text)
                
        description_value = metadata_payload.get("description") or metadata_payload.get("summary")
        if description_value:
            description_text = _stringify_metadata_value(description_value)
            if description_text:
                metadata_overrides.setdefault("description", description_text)
                metadata_overrides.setdefault("summary", description_text)

        subtitle_value = metadata_payload.get("subtitle")
        if subtitle_value:
            subtitle_text = _stringify_metadata_value(subtitle_value)
            if subtitle_text:
                metadata_overrides.setdefault("subtitle", subtitle_text)

        publisher_value = metadata_payload.get("publisher")
        if publisher_value:
            publisher_text = _stringify_metadata_value(publisher_value)
            if publisher_text:
                metadata_overrides.setdefault("publisher", publisher_text)

    settings = load_settings()
    integrations = load_integration_settings()
    calibre_settings = integrations.get("calibre_opds", {})
    
    try:
        client = CalibreOPDSClient(
            base_url=calibre_settings.get("base_url") or "",
            username=calibre_settings.get("username"),
            password=calibre_settings.get("password"),
            verify=bool(calibre_settings.get("verify_ssl", True)),
        )
        
        temp_dir = Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))
        temp_dir.mkdir(exist_ok=True)
        
        resource = client.download(href)
        filename = resource.filename
        content = resource.content
        
        if not filename:
            filename = f"{uuid.uuid4().hex}.epub"
            
        file_path = temp_dir / f"{uuid.uuid4().hex}_{filename}"
        file_path.write_bytes(content)
        
        extraction = extract_from_path(file_path)
        
        if metadata_overrides:
            extraction.metadata.update(metadata_overrides)
            
        result = build_pending_job_from_extraction(
            stored_path=file_path,
            original_name=filename,
            extraction=extraction,
            form={},
            settings=settings,
            profiles=serialize_profiles(),
            metadata_overrides=metadata_overrides,
        )
        
        get_service().store_pending_job(result.pending)
        
        return jsonify({
            "success": True,
            "status": "imported",
            "pending_id": result.pending.id,
            "redirect_url": url_for("main.wizard_step", step="book", pending_id=result.pending.id)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- LLM Routes ---

@api_bp.post("/llm/models")
def api_llm_models() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False) or {}
    current_settings = load_settings()

    base_url = str(payload.get("base_url") or payload.get("llm_base_url") or current_settings.get("llm_base_url") or "").strip()
    if not base_url:
        return jsonify({"error": "LLM base URL is required."}), 400

    api_key = str(payload.get("api_key") or payload.get("llm_api_key") or current_settings.get("llm_api_key") or "")
    timeout = coerce_float(payload.get("timeout"), current_settings.get("llm_timeout", 30.0))

    overrides = {
        "llm_base_url": base_url,
        "llm_api_key": api_key,
        "llm_timeout": timeout,
    }

    merged = apply_overrides(current_settings, overrides)
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

    base_settings = load_settings()
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
        "llm_timeout": coerce_float(payload.get("timeout"), base_settings.get("llm_timeout", 30.0)),
        "normalization_apostrophe_mode": "llm",
    }

    merged = apply_overrides(base_settings, overrides)
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

# --- Normalization Routes ---

@api_bp.post("/normalization/preview")
def api_normalization_preview() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=False) or {}
    sample_text = str(payload.get("text") or "").strip()
    if not sample_text:
        return jsonify({"error": "Sample text is required."}), 400

    base_settings = load_settings()
    # We might want to apply overrides from payload if any normalization settings are passed
    # For now, just use base settings as in original code (presumably)
    
    apostrophe_config = build_apostrophe_config(settings=base_settings)
    try:
        normalized_text = normalize_for_pipeline(sample_text, config=apostrophe_config, settings=base_settings)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify({
        "text": sample_text,
        "normalized_text": normalized_text,
    })

@api_bp.post("/entity-pronunciation/preview")
def api_entity_pronunciation_preview() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=True) or {}
    token = payload.get("token", "").strip()
    pronunciation = payload.get("pronunciation", "").strip()
    voice = payload.get("voice", "").strip()
    language = payload.get("language", "a").strip()
    
    if not token and not pronunciation:
        return jsonify({"error": "Token or pronunciation required"}), 400
        
    text_to_speak = pronunciation if pronunciation else token
    
    if not voice:
        settings = load_settings()
        voice = settings.get("default_voice", "af_heart")
        
    try:
        # Check GPU setting
        settings = load_settings()
        use_gpu = coerce_bool(settings.get("use_gpu"), False)
        
        audio_bytes = generate_preview_audio(
            text=text_to_speak,
            voice_spec=voice,
            language=language,
            speed=1.0,
            use_gpu=use_gpu,
        )
        audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")
        return jsonify({"audio_base64": audio_base64})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
