from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Mapping


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class JobLog:
    timestamp: float
    message: str
    level: str = "info"


@dataclass
class JobResult:
    audio_path: Optional[Path] = None
    subtitle_paths: List[Path] = field(default_factory=list)
    artifacts: Dict[str, Path] = field(default_factory=dict)


@dataclass
class Job:
    id: str
    original_filename: str
    stored_path: Path
    language: str
    voice: str
    speed: float
    use_gpu: bool
    subtitle_mode: str
    output_format: str
    save_mode: str
    output_folder: Optional[Path]
    replace_single_newlines: bool
    subtitle_format: str
    created_at: float
    save_chapters_separately: bool = False
    merge_chapters_at_end: bool = True
    separate_chapters_format: str = "wav"
    silence_between_chapters: float = 2.0
    save_as_project: bool = False
    voice_profile: Optional[str] = None
    metadata_tags: Dict[str, str] = field(default_factory=dict)
    max_subtitle_words: int = 50
    status: JobStatus = JobStatus.PENDING
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress: float = 0.0
    total_characters: int = 0
    processed_characters: int = 0
    logs: List[JobLog] = field(default_factory=list)
    error: Optional[str] = None
    result: JobResult = field(default_factory=JobResult)
    chapters: List[Dict[str, Any]] = field(default_factory=list)
    queue_position: Optional[int] = None
    cancel_requested: bool = False

    def add_log(self, message: str, level: str = "info") -> None:
        self.logs.append(JobLog(timestamp=time.time(), message=message, level=level))

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "original_filename": self.original_filename,
            "status": self.status.value,
            "use_gpu": self.use_gpu,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "total_characters": self.total_characters,
            "processed_characters": self.processed_characters,
            "error": self.error,
            "logs": [log.__dict__ for log in self.logs],
            "result": {
                "audio": str(self.result.audio_path) if self.result.audio_path else None,
                "subtitles": [str(path) for path in self.result.subtitle_paths],
                "artifacts": {key: str(path) for key, path in self.result.artifacts.items()},
            },
            "queue_position": self.queue_position,
            "options": {
                "save_chapters_separately": self.save_chapters_separately,
                "merge_chapters_at_end": self.merge_chapters_at_end,
                "separate_chapters_format": self.separate_chapters_format,
                "silence_between_chapters": self.silence_between_chapters,
                "save_as_project": self.save_as_project,
                "voice_profile": self.voice_profile,
                "max_subtitle_words": self.max_subtitle_words,
            },
            "metadata_tags": dict(self.metadata_tags),
            "chapters": [
                {
                    "id": entry.get("id"),
                    "index": entry.get("index"),
                    "order": entry.get("order"),
                    "title": entry.get("title"),
                    "enabled": bool(entry.get("enabled", True)),
                    "characters": len(str(entry.get("text", ""))),
                }
                for entry in self.chapters
            ],
        }


class ConversionService:
    def __init__(
        self,
        output_root: Path,
        runner: Callable[[Job], None],
        *,
        uploads_root: Optional[Path] = None,
        poll_interval: float = 0.5,
    ) -> None:
        self._jobs: Dict[str, Job] = {}
        self._queue: List[str] = []
        self._lock = threading.RLock()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._output_root = output_root
        self._uploads_root = uploads_root or output_root / "uploads"
        self._runner = runner
        self._poll_interval = poll_interval
        self._ensure_directories()

    # Public API ---------------------------------------------------------
    def list_jobs(self) -> List[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def enqueue(
        self,
        *,
        original_filename: str,
        stored_path: Path,
        language: str,
        voice: str,
        speed: float,
        use_gpu: bool,
        subtitle_mode: str,
        output_format: str,
        save_mode: str,
        output_folder: Optional[Path],
        replace_single_newlines: bool,
        subtitle_format: str,
        total_characters: int,
        chapters: Optional[Iterable[Any]] = None,
        save_chapters_separately: bool = False,
        merge_chapters_at_end: bool = True,
        separate_chapters_format: str = "wav",
        silence_between_chapters: float = 2.0,
        save_as_project: bool = False,
        voice_profile: Optional[str] = None,
        max_subtitle_words: int = 50,
        metadata_tags: Optional[Mapping[str, Any]] = None,
    ) -> Job:
        job_id = uuid.uuid4().hex
        normalized_metadata = self._normalize_metadata_tags(metadata_tags)
        normalized_chapters = self._normalize_chapters(chapters)
        if total_characters <= 0 and normalized_chapters:
            total_characters = sum(len(str(entry.get("text", ""))) for entry in normalized_chapters)
        job = Job(
            id=job_id,
            original_filename=original_filename,
            stored_path=stored_path,
            language=language,
            voice=voice,
            speed=speed,
            use_gpu=use_gpu,
            subtitle_mode=subtitle_mode,
            output_format=output_format,
            save_mode=save_mode,
            output_folder=output_folder,
            replace_single_newlines=replace_single_newlines,
            subtitle_format=subtitle_format,
            save_chapters_separately=save_chapters_separately,
            merge_chapters_at_end=merge_chapters_at_end,
            separate_chapters_format=separate_chapters_format,
            silence_between_chapters=silence_between_chapters,
            save_as_project=save_as_project,
            voice_profile=voice_profile,
            max_subtitle_words=max_subtitle_words,
            metadata_tags=normalized_metadata,
            created_at=time.time(),
            total_characters=total_characters,
            chapters=normalized_chapters,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._queue.append(job_id)
            self._update_queue_positions_locked()
            self._wake_event.set()
        self._ensure_worker()
        job.add_log("Job queued")
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return False
            job.cancel_requested = True
            job.add_log("Cancellation requested", level="warning")
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.CANCELLED
                self._queue.remove(job_id)
                job.finished_at = time.time()
                self._update_queue_positions_locked()
            return True

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in {JobStatus.RUNNING}:
                return False
            self._jobs.pop(job_id)
            if job_id in self._queue:
                self._queue.remove(job_id)
                self._update_queue_positions_locked()
            return True

    def clear_finished(self, *, statuses: Optional[Iterable[JobStatus]] = None) -> int:
        finished_statuses = set(statuses or {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
        removed = 0
        with self._lock:
            # Remove any queued entries first to avoid stale references
            filtered_queue: List[str] = []
            for job_id in self._queue:
                job = self._jobs.get(job_id)
                if job and job.status in finished_statuses:
                    continue
                filtered_queue.append(job_id)
            self._queue = filtered_queue

            for job_id, job in list(self._jobs.items()):
                if job.status in finished_statuses:
                    self._jobs.pop(job_id)
                    removed += 1

            if removed:
                self._update_queue_positions_locked()
        return removed

    def shutdown(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5)
            self._worker_thread = None

    # Internal -----------------------------------------------------------
    def _ensure_directories(self) -> None:
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._uploads_root.mkdir(parents=True, exist_ok=True)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._stop_event.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="abogen-conversion-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            job = None
            with self._lock:
                self._wake_event.clear()
                while self._queue and self._jobs[self._queue[0]].status in {
                    JobStatus.CANCELLED,
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                }:
                    self._queue.pop(0)
                if self._queue:
                    job = self._jobs[self._queue.pop(0)]
                else:
                    self._update_queue_positions_locked()
            if job is None:
                self._wake_event.wait(timeout=self._poll_interval)
                continue
            if job.cancel_requested:
                job.add_log("Job cancelled before start", level="warning")
                job.status = JobStatus.CANCELLED
                job.finished_at = time.time()
                continue
            self._run_job(job)

    def _run_job(self, job: Job) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        job.add_log("Job started", level="info")
        try:
            self._runner(job)
        except Exception as exc:  # pragma: no cover - defensive
            job.error = str(exc)
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            job.add_log(f"Job failed: {exc}", level="error")
        else:
            if job.cancel_requested:
                job.status = JobStatus.CANCELLED
                job.add_log("Job cancelled", level="warning")
            elif job.status != JobStatus.FAILED:
                job.status = JobStatus.COMPLETED
                job.add_log("Job completed", level="success")
            job.finished_at = time.time()
        finally:
            with self._lock:
                self._update_queue_positions_locked()

    def _update_queue_positions_locked(self) -> None:
        for index, job_id in enumerate(self._queue, start=1):
            job = self._jobs.get(job_id)
            if job:
                job.queue_position = index

    @staticmethod
    def _coerce_bool(value: Any, default: bool = True) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
            return default
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _coerce_optional_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_metadata_tags(values: Optional[Mapping[str, Any]]) -> Dict[str, str]:
        if not values:
            return {}
        normalized: Dict[str, str] = {}
        for key, raw_value in values.items():
            if raw_value is None:
                continue
            key_str = str(key).strip()
            if not key_str:
                continue
            normalized[key_str] = str(raw_value)
        return normalized

    @classmethod
    def _normalize_chapters(cls, chapters: Optional[Iterable[Any]]) -> List[Dict[str, Any]]:
        if not chapters:
            return []

        normalized: List[Dict[str, Any]] = []
        for order, raw in enumerate(chapters):
            if raw is None:
                continue

            if isinstance(raw, str):
                raw_dict: Dict[str, Any] = {"title": raw}
            elif isinstance(raw, dict):
                raw_dict = dict(raw)
            else:
                continue

            entry: Dict[str, Any] = {}

            id_value = raw_dict.get("id") or raw_dict.get("chapter_id") or raw_dict.get("key")
            if id_value is not None:
                entry["id"] = str(id_value)

            index_value = (
                cls._coerce_optional_int(raw_dict.get("index"))
                or cls._coerce_optional_int(raw_dict.get("original_index"))
                or cls._coerce_optional_int(raw_dict.get("source_index"))
                or cls._coerce_optional_int(raw_dict.get("chapter_index"))
            )
            if index_value is not None:
                entry["index"] = index_value

            order_value = (
                cls._coerce_optional_int(raw_dict.get("order"))
                or cls._coerce_optional_int(raw_dict.get("position"))
                or cls._coerce_optional_int(raw_dict.get("sort"))
                or cls._coerce_optional_int(raw_dict.get("sort_order"))
            )
            entry["order"] = order_value if order_value is not None else order

            source_title = (
                raw_dict.get("source_title")
                or raw_dict.get("original_title")
                or raw_dict.get("base_title")
            )
            if source_title:
                entry["source_title"] = str(source_title)

            title_value = (
                raw_dict.get("title")
                or raw_dict.get("name")
                or raw_dict.get("label")
                or raw_dict.get("chapter")
            )
            if title_value is not None:
                entry["title"] = str(title_value)
            elif source_title:
                entry["title"] = str(source_title)
            else:
                entry["title"] = f"Chapter {order + 1}"

            text_value = raw_dict.get("text")
            if text_value is None:
                text_value = raw_dict.get("content") or raw_dict.get("body") or raw_dict.get("value")
            if text_value is not None:
                entry["text"] = str(text_value)

            enabled = cls._coerce_bool(
                raw_dict.get("enabled", raw_dict.get("include", raw_dict.get("selected", True))),
                True,
            )
            if "disabled" in raw_dict and cls._coerce_bool(raw_dict.get("disabled"), False):
                enabled = False
            entry["enabled"] = enabled

            metadata_payload = raw_dict.get("metadata") or raw_dict.get("metadata_tags")
            normalized_metadata = cls._normalize_metadata_tags(metadata_payload)
            if normalized_metadata:
                entry["metadata"] = normalized_metadata

            normalized.append(entry)

        return normalized


def default_storage_root() -> Path:
    base = Path.cwd()
    uploads = base / "var" / "uploads"
    outputs = base / "var" / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    uploads.mkdir(parents=True, exist_ok=True)
    return outputs


def build_service(
    runner: Callable[[Job], None],
    *,
    output_root: Optional[Path] = None,
    uploads_root: Optional[Path] = None,
) -> ConversionService:
    output_root = output_root or default_storage_root()
    service = ConversionService(
        output_root=output_root,
        uploads_root=uploads_root,
        runner=runner,
    )
    return service
