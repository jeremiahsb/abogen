from __future__ import annotations

import json
import logging
import mimetypes
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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
    folder_id: Optional[str] = None
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
    """Client for the legacy Audiobookshelf multipart upload endpoint."""

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
        if not self._config.folder_id:
            raise AudiobookshelfUploadError("Audiobookshelf folder ID is required for uploads")

        form_fields = self._build_upload_fields(audio_path, metadata, chapters)
        file_entries = self._build_file_entries(audio_path, cover_path, subtitles)

        route = self._api_path("upload")
        try:
            with self._open_client() as client, ExitStack() as stack:
                files_payload = self._open_file_handles(file_entries, stack)
                response = client.post(route, data=form_fields, files=files_payload)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = (exc.response.text or "").strip()
            if detail:
                detail = detail[:200]
                message = f"Audiobookshelf upload failed with status {status}: {detail}"
            else:
                message = f"Audiobookshelf upload failed with status {status}"
            raise AudiobookshelfUploadError(
                message
            ) from exc
        except httpx.HTTPError as exc:
            raise AudiobookshelfUploadError(f"Audiobookshelf upload failed: {exc}") from exc

        return {}

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

    def _build_upload_fields(
        self,
        audio_path: Path,
        metadata: Dict[str, Any],
        chapters: Optional[Iterable[Dict[str, Any]]],
    ) -> Dict[str, str]:
        title = self._extract_title(metadata, audio_path)
        author = self._extract_author(metadata)
        series = self._extract_series(metadata)

        fields: Dict[str, str] = {
            "library": self._config.library_id,
            "folder": self._config.folder_id or "",
            "title": title,
        }
        if author:
            fields["author"] = author
        if series:
            fields["series"] = series
        if self._config.collection_id:
            fields["collectionId"] = self._config.collection_id

        metadata_payload: Dict[str, Any] = metadata or {}
        if chapters and self._config.send_chapters:
            metadata_payload = dict(metadata_payload)
            metadata_payload["chapters"] = list(chapters)

        if metadata_payload:
            try:
                fields["metadata"] = json.dumps(metadata_payload, ensure_ascii=False)
            except (TypeError, ValueError):
                logger.debug("Failed to serialize Audiobookshelf metadata payload")

        return fields

    def _build_file_entries(
        self,
        audio_path: Path,
        cover_path: Optional[Path],
        subtitles: Optional[Iterable[Path]],
    ) -> List[Tuple[str, Path]]:
        entries: List[Tuple[str, Path]] = [("file0", audio_path)]
        index = 1

        if cover_path and self._config.send_cover and cover_path.exists():
            entries.append((f"file{index}", cover_path))
            index += 1

        if subtitles and self._config.send_subtitles:
            for subtitle in subtitles:
                if subtitle.exists():
                    entries.append((f"file{index}", subtitle))
                    index += 1

        return entries

    def _open_file_handles(
        self,
        entries: Sequence[Tuple[str, Path]],
        stack: ExitStack,
    ) -> List[Tuple[str, Tuple[str, Any, str]]]:
        files: List[Tuple[str, Tuple[str, Any, str]]] = []
        for field_name, path in entries:
            mime_type, _ = mimetypes.guess_type(path.name)
            mime_type = mime_type or "application/octet-stream"
            handle = stack.enter_context(path.open("rb"))
            files.append((field_name, (path.name, handle, mime_type)))
        return files

    @staticmethod
    def _extract_title(metadata: Mapping[str, Any], audio_path: Path) -> str:
        title = metadata.get("title") if isinstance(metadata, Mapping) else None
        candidate = str(title).strip() if isinstance(title, str) else ""
        if candidate:
            return candidate
        return audio_path.stem or audio_path.name

    @staticmethod
    def _extract_author(metadata: Mapping[str, Any]) -> str:
        authors = metadata.get("authors") if isinstance(metadata, Mapping) else None
        if isinstance(authors, str):
            candidate = authors.strip()
            return candidate
        if isinstance(authors, Iterable) and not isinstance(authors, (str, Mapping)):
            names = [str(entry).strip() for entry in authors if isinstance(entry, str) and entry.strip()]
            if names:
                return ", ".join(names)
        return ""

    @staticmethod
    def _extract_series(metadata: Mapping[str, Any]) -> str:
        series_name = metadata.get("seriesName") if isinstance(metadata, Mapping) else None
        if isinstance(series_name, str) and series_name.strip():
            return series_name.strip()
        return ""
