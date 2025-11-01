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
        self._folder_cache: Optional[Tuple[str, str, str]] = None

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
        folder_id, _, _ = self._ensure_folder()
        title = self._extract_title(metadata, audio_path)
        author = self._extract_author(metadata)
        series = self._extract_series(metadata)

        fields: Dict[str, str] = {
            "library": self._config.library_id,
            "folder": folder_id,
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

    def resolve_folder(self) -> Tuple[str, str, str]:
        """Return the resolved folder (id, name, library name)."""
        return self._ensure_folder()

    def list_folders(self) -> List[Dict[str, str]]:
        """Return all folders for the configured library."""
        library_name, folders = self._load_library_metadata()
        results: List[Dict[str, str]] = []
        for folder in folders:
            folder_id = str(folder.get("id") or "").strip()
            if not folder_id:
                continue
            name = self._folder_display_name(folder)
            path = self._select_folder_path(folder)
            results.append(
                {
                    "id": folder_id,
                    "name": name,
                    "path": path,
                    "library": library_name,
                }
            )
        results.sort(key=lambda entry: (entry.get("path") or entry.get("name") or entry.get("id") or "").lower())
        return results

    def _ensure_folder(self) -> Tuple[str, str, str]:
        if self._folder_cache:
            return self._folder_cache

        identifier = (self._config.folder_id or "").strip()
        if not identifier:
            raise AudiobookshelfUploadError(
                "Audiobookshelf folder is required; enter the folder name or ID in Settings."
            )

        identifier_norm = self._normalize_identifier(identifier)
        library_name, folders = self._load_library_metadata()

        # direct ID match
        for folder in folders:
            folder_id = str(folder.get("id") or "").strip()
            if folder_id and folder_id == identifier:
                folder_name = self._folder_display_name(folder) or folder_id
                self._folder_cache = (folder_id, folder_name, library_name)
                return self._folder_cache

        has_path_component = "/" in identifier_norm

        for folder in folders:
            folder_id = str(folder.get("id") or "").strip()
            if not folder_id:
                continue
            folder_name = self._folder_display_name(folder)
            name_norm = self._normalize_identifier(folder_name)
            if name_norm and name_norm == identifier_norm:
                self._folder_cache = (folder_id, folder_name or folder_id, library_name)
                return self._folder_cache

            for candidate in self._folder_path_candidates(folder):
                candidate_norm = self._normalize_identifier(candidate)
                if not candidate_norm:
                    continue
                if candidate_norm == identifier_norm:
                    self._folder_cache = (folder_id, folder_name or folder_id, library_name)
                    return self._folder_cache
                if has_path_component and candidate_norm.endswith(identifier_norm):
                    self._folder_cache = (folder_id, folder_name or folder_id, library_name)
                    return self._folder_cache
                if not has_path_component:
                    tail = candidate_norm.split("/")[-1]
                    if tail and tail == identifier_norm:
                        self._folder_cache = (folder_id, folder_name or folder_id, library_name)
                        return self._folder_cache

        raise AudiobookshelfUploadError(
            f"Folder '{identifier}' was not found in library '{library_name}'. "
            "Enter the folder name exactly as it appears in Audiobookshelf, a trailing path segment, or paste the folder ID."
        )

    def _load_library_metadata(self) -> Tuple[str, List[Mapping[str, Any]]]:
        try:
            with self._open_client() as client:
                response = client.get(self._api_path(f"libraries/{self._config.library_id}"))
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                message = f"Audiobookshelf library '{self._config.library_id}' not found."
            else:
                detail = (exc.response.text or "").strip()
                if detail:
                    detail = detail[:200]
                    message = (
                        f"Failed to load Audiobookshelf library '{self._config.library_id}' "
                        f"(status {status}): {detail}"
                    )
                else:
                    message = (
                        f"Failed to load Audiobookshelf library '{self._config.library_id}' "
                        f"(status {status})."
                    )
            raise AudiobookshelfUploadError(message) from exc
        except httpx.HTTPError as exc:
            raise AudiobookshelfUploadError(
                f"Failed to reach Audiobookshelf library '{self._config.library_id}': {exc}"
            ) from exc

        if not isinstance(payload, Mapping):
            return self._config.library_id, []

        library_name = str(payload.get("name") or payload.get("label") or self._config.library_id)
        raw_folders = payload.get("libraryFolders") or payload.get("folders") or []
        folders = [entry for entry in raw_folders if isinstance(entry, Mapping)]
        return library_name, folders

    @staticmethod
    def _folder_path_candidates(folder: Mapping[str, Any]) -> List[str]:
        candidates: List[str] = []
        for key in ("fullPath", "fullpath", "path", "folderPath", "virtualPath"):
            value = folder.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value)
        return candidates

    @staticmethod
    def _folder_display_name(folder: Mapping[str, Any]) -> str:
        name = str(folder.get("name") or folder.get("label") or "").strip()
        if name:
            return name
        path = AudiobookshelfClient._select_folder_path(folder)
        if path:
            tail = path.strip("/ ")
            tail = tail.split("/")[-1] if tail else ""
            if tail:
                return tail
        return str(folder.get("id") or "").strip()

    @staticmethod
    def _select_folder_path(folder: Mapping[str, Any]) -> str:
        for candidate in AudiobookshelfClient._folder_path_candidates(folder):
            normalized = candidate.replace("\\", "/").strip()
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _normalize_identifier(value: str) -> str:
        token = (value or "").strip()
        token = token.replace("\\", "/")
        if len(token) > 1 and token[1] == ":":
            token = token[2:]
        token = token.strip("/ ")
        return token.lower()

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
