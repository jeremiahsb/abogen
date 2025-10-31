from __future__ import annotations

import json
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)


class AudiobookshelfUploadError(RuntimeError):
    """Raised when an upload to Audiobookshelf fails."""


@dataclass(frozen=True)
class AudiobookshelfConfig:
    base_url: str
    api_token: str
    library_id: str
    collection_id: Optional[str] = None
    verify_ssl: bool = True
    send_cover: bool = True
    send_chapters: bool = True
    send_subtitles: bool = True
    timeout: float = 30.0

    def normalized_base_url(self) -> str:
        base = (self.base_url or "").strip()
        if not base:
            raise ValueError("Audiobookshelf base URL is required")
        normalized = base.rstrip("/")
        # The web UI historically suggested including '/api' in the base URL; trim
        # it here so we can safely append `/api/...` endpoints below.
        if normalized.lower().endswith("/api"):
            normalized = normalized[:-4]
        return normalized or base


class AudiobookshelfClient:
    """Minimal client for Audiobookshelf's upload API."""

    def __init__(self, config: AudiobookshelfConfig) -> None:
        if not config.api_token:
            raise ValueError("Audiobookshelf API token is required")
        if not config.library_id:
            raise ValueError("Audiobookshelf library ID is required")
        self._config = config
        normalized = config.normalized_base_url() or ""
        self._base_url = normalized.rstrip("/") or normalized
        self._client_base_url = f"{self._base_url}/"

    def _api_path(self, suffix: str = "") -> str:
        """Join the API prefix with the provided suffix without losing proxies."""
        clean_suffix = suffix.lstrip("/")
        return f"api/{clean_suffix}" if clean_suffix else "api"

    def upload_audiobook(
        self,
        audio_path: Path,
        *,
        metadata: Dict[str, Any],
        cover_path: Optional[Path] = None,
        chapters: Optional[Iterable[Dict[str, Any]]] = None,
        subtitles: Optional[Iterable[Path]] = None,
    ) -> Dict[str, Any]:
        if not audio_path.exists():
            raise AudiobookshelfUploadError(f"Audio path does not exist: {audio_path}")
        session = self._create_upload_session()
        upload_id = session.get("id") or session.get("uploadId") or session.get("upload_id")
        if not upload_id:
            raise AudiobookshelfUploadError("Audiobookshelf upload session did not return an identifier")
        logger.debug("Audiobookshelf upload session %s created", upload_id)

        self._upload_file(upload_id, audio_path, kind="audio")

        if cover_path and self._config.send_cover and cover_path.exists():
            try:
                self._upload_file(upload_id, cover_path, kind="cover")
            except AudiobookshelfUploadError:
                logger.warning("Failed to upload cover to Audiobookshelf; continuing without cover.")

        if subtitles and self._config.send_subtitles:
            for subtitle in subtitles:
                if not subtitle.exists():
                    continue
                try:
                    self._upload_file(upload_id, subtitle, kind="subtitle")
                except AudiobookshelfUploadError:
                    logger.warning("Failed to upload subtitle %s to Audiobookshelf", subtitle)

        payload = self._build_finalize_payload(metadata, chapters)
        result = self._finalize_upload(upload_id, payload)
        logger.debug("Audiobookshelf upload %s finalized", upload_id)
        return result

    def _open_client(self) -> httpx.Client:
        headers = {
            "Authorization": f"Bearer {self._config.api_token}",
            "Accept": "application/json",
        }
        return httpx.Client(
            base_url=self._client_base_url,
            headers=headers,
            timeout=self._config.timeout,
            verify=self._config.verify_ssl,
        )

    def _create_upload_session(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "libraryId": self._config.library_id,
            "mediaType": "audiobook",
        }
        if self._config.collection_id:
            payload["collectionId"] = self._config.collection_id
        try:
            with self._open_client() as client:
                response = client.post(self._api_path("uploads"), json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise AudiobookshelfUploadError(f"Unable to create Audiobookshelf upload session: {exc}") from exc

    def _upload_file(self, upload_id: str, path: Path, *, kind: str) -> None:
        mime_type, _ = mimetypes.guess_type(path.name)
        mime_type = mime_type or "application/octet-stream"
        data = {"kind": kind, "filename": path.name}
        route = self._api_path(f"uploads/{upload_id}/files")
        try:
            with path.open("rb") as handle:
                files = {"file": (path.name, handle, mime_type)}
                with self._open_client() as client:
                    response = client.post(route, data=data, files=files)
                    response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AudiobookshelfUploadError(f"Audiobookshelf file upload failed for {path.name}: {exc}") from exc

    def _build_finalize_payload(
        self,
        metadata: Dict[str, Any],
        chapters: Optional[Iterable[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "metadata": metadata or {},
        }
        if chapters and self._config.send_chapters:
            payload["chapters"] = list(chapters)
        return payload

    def _finalize_upload(self, upload_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        route = self._api_path(f"uploads/{upload_id}/finish")
        try:
            with self._open_client() as client:
                response = client.post(route, json=payload)
                response.raise_for_status()
                if response.content:
                    return json.loads(response.content.decode("utf-8"))
        except httpx.HTTPStatusError as exc:
            raise AudiobookshelfUploadError(
                f"Audiobookshelf finalize request failed with status {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AudiobookshelfUploadError(f"Audiobookshelf finalize request failed: {exc}") from exc
        except json.JSONDecodeError:
            logger.debug("Audiobookshelf finalize response was not JSON; returning empty object")
        return {}
