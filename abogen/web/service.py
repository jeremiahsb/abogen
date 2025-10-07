from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Mapping

from abogen.utils import get_internal_cache_path


def _create_set_event() -> threading.Event:
    event = threading.Event()
    event.set()
    return event


STATE_VERSION = 3


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
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
    chapter_intro_delay: float = 0.5
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
    pause_requested: bool = False
    paused: bool = False
    resume_token: Optional[str] = None
    pause_event: threading.Event = field(default_factory=_create_set_event, repr=False, compare=False)
    cover_image_path: Optional[Path] = None
    cover_image_mime: Optional[str] = None

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
                "chapter_intro_delay": self.chapter_intro_delay,
            },
            "metadata_tags": dict(self.metadata_tags),
            "chapters": [
                {
                    "id": entry.get("id"),
                    "index": entry.get("index"),
                    "order": entry.get("order"),
                    "title": entry.get("title"),
                    "enabled": bool(entry.get("enabled", True)),
                    "voice": entry.get("voice"),
                    "voice_profile": entry.get("voice_profile"),
                    "voice_formula": entry.get("voice_formula"),
                    "resolved_voice": entry.get("resolved_voice"),
                    "characters": len(str(entry.get("text", ""))),
                }
                for entry in self.chapters
            ],
        }


@dataclass
class PendingJob:
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
    total_characters: int
    save_chapters_separately: bool
    merge_chapters_at_end: bool
    separate_chapters_format: str
    silence_between_chapters: float
    save_as_project: bool
    voice_profile: Optional[str]
    max_subtitle_words: int
    metadata_tags: Dict[str, Any]
    chapters: List[Dict[str, Any]]
    created_at: float
    cover_image_path: Optional[Path] = None
    cover_image_mime: Optional[str] = None
    chapter_intro_delay: float = 0.5


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
        self._pending_jobs: Dict[str, PendingJob] = {}
        self._state_path = Path(get_internal_cache_path("jobs")) / "queue_state.json"
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_directories()
        self._load_state()

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
        cover_image_path: Optional[Path] = None,
        cover_image_mime: Optional[str] = None,
        chapter_intro_delay: float = 0.5,
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
            cover_image_path=cover_image_path,
            cover_image_mime=cover_image_mime,
            chapter_intro_delay=chapter_intro_delay,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._queue.append(job_id)
            self._update_queue_positions_locked()
            self._wake_event.set()
        self._ensure_worker()
        job.add_log("Job queued")
        return job

    def store_pending_job(self, pending: PendingJob) -> None:
        with self._lock:
            self._pending_jobs[pending.id] = pending

    def get_pending_job(self, pending_id: str) -> Optional[PendingJob]:
        with self._lock:
            return self._pending_jobs.get(pending_id)

    def pop_pending_job(self, pending_id: str) -> Optional[PendingJob]:
        with self._lock:
            return self._pending_jobs.pop(pending_id, None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return False
            job.cancel_requested = True
            job.pause_requested = False
            job.paused = False
            job.add_log("Cancellation requested", level="warning")
            job.pause_event.set()
            if job.status == JobStatus.PENDING:
                job.status = JobStatus.CANCELLED
                self._queue.remove(job_id)
                job.finished_at = time.time()
                self._update_queue_positions_locked()
            self._persist_state()
            return True

    def pause(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return False
            if job.pause_requested or job.paused:
                return True

            job.pause_requested = True
            job.add_log("Pause requested; finishing current chunk before stopping.", level="warning")

            if job.status == JobStatus.PENDING:
                if job_id in self._queue:
                    self._queue.remove(job_id)
                    self._update_queue_positions_locked()
                job.status = JobStatus.PAUSED
                job.paused = True
                job.pause_event.clear()
            self._persist_state()
            return True

    def resume(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                return False

            job.pause_requested = False

            if job.status == JobStatus.PAUSED and job.started_at is None:
                job.status = JobStatus.PENDING
                job.paused = False
                job.pause_event.set()
                if job_id not in self._queue:
                    self._queue.insert(0, job_id)
                self._update_queue_positions_locked()
                self._wake_event.set()
                job.add_log("Resume requested; returning job to queue.", level="info")
            else:
                job.paused = False
                job.pause_event.set()
                if job.status == JobStatus.PAUSED:
                    job.status = JobStatus.RUNNING
                job.add_log("Resume requested", level="info")

            self._persist_state()
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
            self._persist_state()
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
            self._persist_state()
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
        job.pause_event.set()
        job.pause_requested = False
        job.paused = False
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        job.add_log("Job started", level="info")
        self._persist_state()
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
            job.pause_event.set()
            self._persist_state()
            with self._lock:
                self._update_queue_positions_locked()

    def _update_queue_positions_locked(self) -> None:
        for index, job_id in enumerate(self._queue, start=1):
            job = self._jobs.get(job_id)
            if job:
                job.queue_position = index
        self._persist_state()

    # Persistence ------------------------------------------------------
    def _serialize_job(self, job: Job) -> Dict[str, Any]:
        result_audio = str(job.result.audio_path) if job.result.audio_path else None
        result_subtitles = [str(path) for path in job.result.subtitle_paths]
        result_artifacts = {key: str(path) for key, path in job.result.artifacts.items()}
        return {
            "id": job.id,
            "original_filename": job.original_filename,
            "stored_path": str(job.stored_path),
            "language": job.language,
            "voice": job.voice,
            "speed": job.speed,
            "use_gpu": job.use_gpu,
            "subtitle_mode": job.subtitle_mode,
            "output_format": job.output_format,
            "save_mode": job.save_mode,
            "output_folder": str(job.output_folder) if job.output_folder else None,
            "replace_single_newlines": job.replace_single_newlines,
            "subtitle_format": job.subtitle_format,
            "created_at": job.created_at,
            "save_chapters_separately": job.save_chapters_separately,
            "merge_chapters_at_end": job.merge_chapters_at_end,
            "separate_chapters_format": job.separate_chapters_format,
            "silence_between_chapters": job.silence_between_chapters,
            "save_as_project": job.save_as_project,
            "voice_profile": job.voice_profile,
            "metadata_tags": job.metadata_tags,
            "max_subtitle_words": job.max_subtitle_words,
            "status": job.status.value,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "progress": job.progress,
            "total_characters": job.total_characters,
            "processed_characters": job.processed_characters,
            "error": job.error,
            "logs": [log.__dict__ for log in job.logs][-500:],
            "result": {
                "audio_path": result_audio,
                "subtitle_paths": result_subtitles,
                "artifacts": result_artifacts,
            },
            "chapters": [dict(entry) for entry in job.chapters],
            "queue_position": job.queue_position,
            "cancel_requested": job.cancel_requested,
            "pause_requested": job.pause_requested,
            "paused": job.paused,
            "resume_token": job.resume_token,
            "cover_image_path": str(job.cover_image_path) if job.cover_image_path else None,
            "cover_image_mime": job.cover_image_mime,
            "chapter_intro_delay": job.chapter_intro_delay,
        }

    def _persist_state(self) -> None:
        try:
            with self._lock:
                snapshot = {
                    "version": STATE_VERSION,
                    "jobs": [self._serialize_job(job) for job in self._jobs.values()],
                    "queue": list(self._queue),
                }
            tmp_path = self._state_path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, indent=2)
            os.replace(tmp_path, self._state_path)
        except Exception:
            # Persistence failures should not disrupt runtime; ignore.
            pass

    def _deserialize_job(self, payload: Dict[str, Any]) -> Job:
        stored_path = Path(payload["stored_path"])
        output_folder_raw = payload.get("output_folder")
        output_folder = Path(output_folder_raw) if output_folder_raw else None
        job = Job(
            id=payload["id"],
            original_filename=payload["original_filename"],
            stored_path=stored_path,
            language=payload.get("language", "a"),
            voice=payload.get("voice", ""),
            speed=float(payload.get("speed", 1.0)),
            use_gpu=bool(payload.get("use_gpu", True)),
            subtitle_mode=payload.get("subtitle_mode", "Disabled"),
            output_format=payload.get("output_format", "wav"),
            save_mode=payload.get("save_mode", "Save next to input file"),
            output_folder=output_folder,
            replace_single_newlines=bool(payload.get("replace_single_newlines", False)),
            subtitle_format=payload.get("subtitle_format", "srt"),
            created_at=float(payload.get("created_at", time.time())),
            save_chapters_separately=bool(payload.get("save_chapters_separately", False)),
            merge_chapters_at_end=bool(payload.get("merge_chapters_at_end", True)),
            separate_chapters_format=payload.get("separate_chapters_format", "wav"),
            silence_between_chapters=float(payload.get("silence_between_chapters", 2.0)),
            save_as_project=bool(payload.get("save_as_project", False)),
            voice_profile=payload.get("voice_profile"),
            metadata_tags=payload.get("metadata_tags", {}),
            max_subtitle_words=int(payload.get("max_subtitle_words", 50)),
            chapter_intro_delay=float(payload.get("chapter_intro_delay", 0.5)),
        )
        job.status = JobStatus(payload.get("status", job.status.value))
        job.started_at = payload.get("started_at")
        job.finished_at = payload.get("finished_at")
        job.progress = float(payload.get("progress", 0.0))
        job.total_characters = int(payload.get("total_characters", 0))
        job.processed_characters = int(payload.get("processed_characters", 0))
        job.error = payload.get("error")
        job.logs = [JobLog(**entry) for entry in payload.get("logs", [])]
        result_payload = payload.get("result", {})
        audio_path_raw = result_payload.get("audio_path")
        job.result.audio_path = Path(audio_path_raw) if audio_path_raw else None
        job.result.subtitle_paths = [Path(item) for item in result_payload.get("subtitle_paths", [])]
        job.result.artifacts = {
            key: Path(value) for key, value in result_payload.get("artifacts", {}).items()
        }
        job.chapters = payload.get("chapters", [])
        job.queue_position = payload.get("queue_position")
        job.cancel_requested = bool(payload.get("cancel_requested", False))
        job.pause_requested = bool(payload.get("pause_requested", False))
        job.paused = bool(payload.get("paused", False))
        job.resume_token = payload.get("resume_token")
        cover_path_raw = payload.get("cover_image_path")
        job.cover_image_path = Path(cover_path_raw) if cover_path_raw else None
        job.cover_image_mime = payload.get("cover_image_mime")
        job.pause_event.set()
        return job

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            with self._state_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return

        if payload.get("version") != STATE_VERSION:
            return

        jobs_payload = payload.get("jobs", [])
        queue_payload = payload.get("queue", [])
        loaded_jobs: Dict[str, Job] = {}
        requeue: List[str] = []

        for entry in jobs_payload:
            try:
                job = self._deserialize_job(entry)
            except Exception:
                continue

            if job.status in {JobStatus.RUNNING, JobStatus.PAUSED}:
                job.status = JobStatus.PENDING
                job.add_log("Job restored after restart: resetting to pending queue.", level="warning")
                job.progress = 0.0
                job.processed_characters = 0
                job.pause_requested = False
                job.paused = False
                job.pause_event.set()
                requeue.append(job.id)
            elif job.status == JobStatus.PENDING:
                requeue.append(job.id)

            loaded_jobs[job.id] = job

        with self._lock:
            self._jobs = loaded_jobs
            self._queue = [job_id for job_id in queue_payload if job_id in loaded_jobs]
            for job_id in requeue:
                if job_id not in self._queue:
                    self._queue.append(job_id)
            self._update_queue_positions_locked()

        if self._queue:
            self._ensure_worker()

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

            voice_value = raw_dict.get("voice")
            if voice_value:
                entry["voice"] = str(voice_value)

            profile_value = raw_dict.get("voice_profile")
            if profile_value:
                entry["voice_profile"] = str(profile_value)

            formula_value = raw_dict.get("voice_formula") or raw_dict.get("formula")
            if formula_value:
                entry["voice_formula"] = str(formula_value)

            resolved_value = raw_dict.get("resolved_voice")
            if resolved_value:
                entry["resolved_voice"] = str(resolved_value)

            if "characters" in raw_dict:
                try:
                    entry["characters"] = int(raw_dict.get("characters", 0))
                except (TypeError, ValueError):
                    entry["characters"] = len(str(entry.get("text", "")))
            else:
                entry["characters"] = len(str(entry.get("text", "")))

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
