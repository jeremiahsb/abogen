from typing import Any, Dict, List, Optional
from flask import Blueprint, render_template, request, jsonify, abort, flash, redirect, url_for
from flask.typing import ResponseReturnValue

from abogen.web.routes.utils.voice import (
    template_options,
    resolve_voice_setting,
    resolve_voice_choice,
    parse_voice_formula,
)
from abogen.web.routes.utils.settings import load_settings, coerce_bool
from abogen.web.routes.utils.preview import synthesize_preview
from abogen.speaker_configs import (
    list_configs,
    get_config,
    load_configs,
    save_configs,
    delete_config,
)
from abogen.constants import VOICES_INTERNAL

voices_bp = Blueprint("voices", __name__)

@voices_bp.get("/")
def voices_list() -> ResponseReturnValue:
    # This might not be a standalone page in the original app, but useful to have.
    # Or maybe it redirects to settings or something.
    # For now, I'll just redirect to settings as voices are managed there usually.
    return redirect(url_for("settings.settings_page"))

@voices_bp.post("/test")
def test_voice() -> ResponseReturnValue:
    text = (request.form.get("text") or "").strip()
    voice = (request.form.get("voice") or "").strip()
    speed = float(request.form.get("speed", 1.0))
    
    # This seems to be the form-based preview
    settings = load_settings()
    use_gpu = coerce_bool(settings.get("use_gpu"), True)
    
    try:
        return synthesize_preview(
            text=text,
            voice_spec=voice,
            language="a", # Default language
            speed=speed,
            use_gpu=use_gpu,
        )
    except Exception as e:
        abort(400, str(e))

@voices_bp.get("/configs")
def speaker_configs() -> ResponseReturnValue:
    return jsonify({"configs": list_configs()})

@voices_bp.post("/configs/save")
def save_speaker_config() -> ResponseReturnValue:
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    config = payload.get("config")
    
    if not name:
        abort(400, "Config name is required")
    if not config:
        abort(400, "Config data is required")
        
    configs = load_configs()
    configs[name] = config
    save_configs(configs)
    return jsonify({"status": "saved", "configs": list_configs()})

@voices_bp.post("/configs/delete")
def delete_speaker_config() -> ResponseReturnValue:
    payload = request.get_json(force=True)
    name = (payload.get("name") or "").strip()
    
    if not name:
        abort(400, "Config name is required")
        
    delete_config(name)
    return jsonify({"status": "deleted", "configs": list_configs()})
