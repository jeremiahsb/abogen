from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Any, Optional

from flask import Flask

from abogen.utils import get_user_cache_path, get_user_output_path

from .conversion_runner import run_conversion_job
from .service import build_service


class _SuppressSuccessfulAccessFilter(logging.Filter):
    """Filter out successful (HTTP 200) werkzeug access logs."""

    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - small utility
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        # Werkzeug access logs include the status code near the end, e.g.
        # "GET /path HTTP/1.1" 200 -
        # Treat any 2xx response as success to suppress.
        return " 200 " not in message and " 201 " not in message and " 204 " not in message


_access_log_filter_attached = False


def _default_dirs() -> tuple[Path, Path]:
    uploads_override = os.environ.get("ABOGEN_UPLOAD_ROOT")
    outputs_override = os.environ.get("ABOGEN_OUTPUT_ROOT")

    if uploads_override:
        uploads = Path(os.path.expanduser(uploads_override)).resolve()
    else:
        uploads = Path(get_user_cache_path("web/uploads"))

    if outputs_override:
        outputs = Path(os.path.expanduser(outputs_override)).resolve()
    else:
        outputs = Path(get_user_output_path("web"))

    uploads.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)
    return uploads, outputs


def create_app(config: Optional[dict[str, Any]] = None) -> Flask:
    uploads_dir, outputs_dir = _default_dirs()

    app = Flask(
        __name__,
        static_folder="static",
        template_folder="templates",
    )
    base_config = {
        "SECRET_KEY": os.environ.get("ABOGEN_SECRET_KEY", os.urandom(16)),
        "UPLOAD_FOLDER": str(uploads_dir),
        "OUTPUT_FOLDER": str(outputs_dir),
        "MAX_CONTENT_LENGTH": 1024 * 1024 * 400,  # 400 MB uploads
    }
    if config:
        base_config.update(config)
    app.config.update(base_config)

    service = build_service(
        runner=run_conversion_job,
        output_root=Path(app.config["OUTPUT_FOLDER"]),
        uploads_root=Path(app.config["UPLOAD_FOLDER"]),
    )
    app.extensions["conversion_service"] = service

    from .routes import web_bp, api_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    atexit.register(service.shutdown)

    global _access_log_filter_attached
    if not _access_log_filter_attached:
        logging.getLogger("werkzeug").addFilter(_SuppressSuccessfulAccessFilter())
        _access_log_filter_attached = True

    return app


def main() -> None:
    app = create_app()
    host = os.environ.get("ABOGEN_HOST", "0.0.0.0")
    port = int(os.environ.get("ABOGEN_PORT", "8808"))
    debug = os.environ.get("ABOGEN_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":  # pragma: no cover
    main()
