from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask.typing import ResponseReturnValue

from abogen.web.routes.utils.settings import (
    load_settings,
    load_integration_settings,
    save_settings,
    coerce_bool,
    coerce_int,
    _NORMALIZATION_BOOLEAN_KEYS,
    _NORMALIZATION_STRING_KEYS,
    _DEFAULT_ANALYSIS_THRESHOLD,
)
from abogen.web.routes.utils.voice import template_options

settings_bp = Blueprint("settings", __name__)

_NORMALIZATION_SAMPLES = {
    "apostrophes": "It's a beautiful day, isn't it? 'Yes,' she said, 'it is.'",
    "currency": "The price is $10.50, but it was £8.00 yesterday.",
    "dates": "On 2023-01-01, we celebrated the new year.",
    "numbers": "There are 123 apples and 456 oranges.",
    "abbreviations": "Dr. Smith lives on Elm St. near the U.S. border.",
}

@settings_bp.get("/")
def settings_page() -> str:
    return render_template(
        "settings.html",
        settings=load_settings(),
        integrations=load_integration_settings(),
        options=template_options(),
        normalization_samples=_NORMALIZATION_SAMPLES,
    )

@settings_bp.post("/update")
def update_settings() -> ResponseReturnValue:
    current = load_settings()
    form = request.form

    # General settings
    current["language"] = (form.get("language") or "en").strip()
    current["default_voice"] = (form.get("default_voice") or "").strip()
    current["output_format"] = (form.get("output_format") or "mp3").strip()
    current["subtitle_mode"] = (form.get("subtitle_mode") or "Disabled").strip()
    current["subtitle_format"] = (form.get("subtitle_format") or "srt").strip()
    current["save_mode"] = (form.get("save_mode") or "save_next_to_input").strip()
    
    current["replace_single_newlines"] = coerce_bool(form.get("replace_single_newlines"), False)
    current["use_gpu"] = coerce_bool(form.get("use_gpu"), False)
    current["save_chapters_separately"] = coerce_bool(form.get("save_chapters_separately"), False)
    current["merge_chapters_at_end"] = coerce_bool(form.get("merge_chapters_at_end"), True)
    current["save_as_project"] = coerce_bool(form.get("save_as_project"), False)
    current["separate_chapters_format"] = (form.get("separate_chapters_format") or "wav").strip()
    
    try:
        current["silence_between_chapters"] = max(0.0, float(form.get("silence_between_chapters", 2.0)))
    except ValueError:
        pass
        
    try:
        current["chapter_intro_delay"] = max(0.0, float(form.get("chapter_intro_delay", 0.5)))
    except ValueError:
        pass
        
    current["read_title_intro"] = coerce_bool(form.get("read_title_intro"), False)
    current["read_closing_outro"] = coerce_bool(form.get("read_closing_outro"), True)
    current["normalize_chapter_opening_caps"] = coerce_bool(form.get("normalize_chapter_opening_caps"), True)
    current["auto_prefix_chapter_titles"] = coerce_bool(form.get("auto_prefix_chapter_titles"), True)
    
    try:
        current["max_subtitle_words"] = max(1, int(form.get("max_subtitle_words", 50)))
    except ValueError:
        pass
        
    current["chunk_level"] = (form.get("chunk_level") or "paragraph").strip()
    current["generate_epub3"] = coerce_bool(form.get("generate_epub3"), False)
    
    current["speaker_analysis_threshold"] = coerce_int(
        form.get("speaker_analysis_threshold"),
        _DEFAULT_ANALYSIS_THRESHOLD,
        minimum=1,
        maximum=25,
    )

    # Normalization settings
    for key in _NORMALIZATION_BOOLEAN_KEYS:
        current[key] = coerce_bool(form.get(key), False)
    for key in _NORMALIZATION_STRING_KEYS:
        current[key] = (form.get(key) or "").strip()

    # Integrations
    # Audiobookshelf
    abs_enabled = coerce_bool(form.get("audiobookshelf_enabled"), False)
    abs_url = (form.get("audiobookshelf_url") or "").strip()
    abs_token = (form.get("audiobookshelf_token") or "").strip()
    abs_library = (form.get("audiobookshelf_library_id") or "").strip()
    abs_folder = (form.get("audiobookshelf_folder_id") or "").strip()
    abs_cover = coerce_bool(form.get("audiobookshelf_send_cover"), True)
    abs_chapters = coerce_bool(form.get("audiobookshelf_send_chapters"), True)
    abs_subtitles = coerce_bool(form.get("audiobookshelf_send_subtitles"), True)
    
    current["integrations"] = current.get("integrations", {})
    current["integrations"]["audiobookshelf"] = {
        "enabled": abs_enabled,
        "url": abs_url,
        "token": abs_token,
        "library_id": abs_library,
        "folder_id": abs_folder,
        "send_cover": abs_cover,
        "send_chapters": abs_chapters,
        "send_subtitles": abs_subtitles,
    }
    
    # Calibre
    calibre_enabled = coerce_bool(form.get("calibre_enabled"), False)
    calibre_url = (form.get("calibre_url") or "").strip()
    calibre_user = (form.get("calibre_username") or "").strip()
    calibre_pass = (form.get("calibre_password") or "").strip()
    calibre_library = (form.get("calibre_library_id") or "").strip()
    
    current["integrations"]["calibre"] = {
        "enabled": calibre_enabled,
        "url": calibre_url,
        "username": calibre_user,
        "password": calibre_pass,
        "library_id": calibre_library,
    }

    save_settings(current)
    flash("Settings updated successfully.", "success")
    return redirect(url_for("settings.settings_page"))
