import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from flask import Blueprint, render_template, request, jsonify, current_app, url_for
from flask.typing import ResponseReturnValue

from abogen.web.routes.utils.settings import (
    load_settings,
    stored_integration_config,
)
from abogen.web.routes.utils.voice import template_options
from abogen.web.routes.utils.form import build_pending_job_from_extraction
from abogen.web.routes.utils.service import get_service
from abogen.integrations.calibre_opds import (
    CalibreOPDSClient,
    CalibreOPDSError,
    feed_to_dict,
)
from abogen.text_extractor import extract_from_path
from abogen.voice_profiles import serialize_profiles

books_bp = Blueprint("books", __name__)

def _calibre_integration_enabled(integrations: Dict[str, Any]) -> bool:
    calibre = integrations.get("calibre_opds", {})
    return bool(calibre.get("enabled") and calibre.get("base_url"))

def _build_calibre_client(payload: Dict[str, Any]) -> CalibreOPDSClient:
    return CalibreOPDSClient(
        base_url=payload.get("base_url") or "",
        username=payload.get("username"),
        password=payload.get("password"),
        verify=bool(payload.get("verify_ssl", True)),
    )

@books_bp.get("/")
def find_books_page() -> ResponseReturnValue:
    settings = load_settings()
    integrations = settings.get("integrations", {})
    return render_template(
        "find_books.html",
        integrations=integrations,
        opds_available=_calibre_integration_enabled(integrations),
        options=template_options(),
        settings=settings,
    )

@books_bp.get("/search")
def search_books() -> ResponseReturnValue:
    # This seems to be handled by the feed endpoint in the original code
    # But let's see if there is a separate search page or if it's all JS driven
    return find_books_page()

@books_bp.get("/calibre/feed")
def calibre_opds_feed() -> ResponseReturnValue:
    settings = load_settings()
    integrations = settings.get("integrations", {})
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
        client = _build_calibre_client(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    href = request.args.get("href", type=str)
    query = request.args.get("q", type=str)
    letter = request.args.get("letter", type=str)
    
    try:
        if letter:
            feed = client.browse_letter(letter, start_href=href)
        elif query:
            feed = client.search(query)
        else:
            feed = client.fetch_feed(href)
    except CalibreOPDSError as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify({
        "feed": feed_to_dict(feed),
        "href": href or "",
        "query": query or "",
    })

@books_bp.post("/calibre/import")
def calibre_opds_import() -> ResponseReturnValue:
    if not request.is_json:
        return jsonify({"error": "Expected JSON payload."}), 400
        
    data = request.get_json(silent=True) or {}
    href = str(data.get("href") or "").strip()
    title = str(data.get("title") or "").strip()
    
    if not href:
        return jsonify({"error": "Download link missing."}), 400

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

    settings = load_settings()
    integrations = settings.get("integrations", {})
    calibre_settings = integrations.get("calibre", {})
    
    payload = {
        "base_url": calibre_settings.get("url"),
        "username": calibre_settings.get("username"),
        "password": calibre_settings.get("password"),
        "verify_ssl": True,
    }
    
    try:
        client = _build_calibre_client(payload)
        temp_dir = Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))
        temp_dir.mkdir(exist_ok=True)
        
        # We don't know the filename yet, so we'll use a temp name and rename later if possible
        # Or rely on content-disposition if the client supports it, but here we just download content
        # The client.download_book returns bytes or path?
        # Let's check CalibreClient.download_book
        
        # Assuming it returns bytes for now based on typical usage
        # But wait, I need to check abogen/integrations/calibre_opds.py
        
        resource = client.download(href)
        filename = resource.filename
        content = resource.content
        
        if not filename:
            filename = f"{uuid.uuid4().hex}.epub" # Default to epub if unknown
            
        file_path = temp_dir / f"{uuid.uuid4().hex}_{filename}"
        file_path.write_bytes(content)
        
        extraction = extract_from_path(file_path)
        
        # Apply metadata overrides to extraction if possible, or pass them to build_pending_job
        if metadata_overrides:
            extraction.metadata.update(metadata_overrides)
            
        result = build_pending_job_from_extraction(
            stored_path=file_path,
            original_name=filename,
            extraction=extraction,
            form={}, # No form data for defaults
            settings=settings,
            profiles=serialize_profiles(),
            metadata_overrides=metadata_overrides,
        )
        
        get_service().store_pending_job(result.pending)
        
        return jsonify({
            "status": "imported",
            "pending_id": result.pending.id,
            "redirect": url_for("main.wizard_step", step="chapters", pending_id=result.pending.id)
        })
        
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
