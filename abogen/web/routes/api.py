from typing import Any, Dict, Mapping, List, Optional

from flask import Blueprint, request, jsonify, send_file
from flask.typing import ResponseReturnValue

from abogen.web.routes.utils.settings import (
    load_settings,
    coerce_float,
)
from abogen.voice_profiles import (
    load_profiles,
    save_profiles,
    delete_profile,
)
from abogen.web.routes.utils.preview import synthesize_preview
from abogen.normalization_settings import (
    build_llm_configuration,
    build_apostrophe_config,
    apply_overrides,
)
from abogen.llm_client import list_models, LLMClientError
from abogen.kokoro_text_normalization import normalize_for_pipeline
from abogen.integrations.audiobookshelf import AudiobookshelfClient, AudiobookshelfConfig

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

@api_bp.post("/integrations/audiobookshelf/folders")
def api_abs_folders() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=True) or {}
    host = payload.get("host")
    token = payload.get("token")
    
    if not host or not token:
        return jsonify({"error": "Host and token are required"}), 400
        
    try:
        config = AudiobookshelfConfig(base_url=host, api_token=token)
        client = AudiobookshelfClient(config)
        folders = client.get_libraries()
        return jsonify({"folders": folders})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@api_bp.post("/integrations/audiobookshelf/test")
def api_abs_test() -> ResponseReturnValue:
    payload = request.get_json(force=True, silent=True) or {}
    host = payload.get("host")
    token = payload.get("token")
    
    if not host or not token:
        return jsonify({"error": "Host and token are required"}), 400
        
    try:
        config = AudiobookshelfConfig(base_url=host, api_token=token)
        client = AudiobookshelfClient(config)
        # Just getting libraries is a good enough test
        client.get_libraries()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

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
