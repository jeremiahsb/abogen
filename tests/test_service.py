from __future__ import annotations

import io
import time
from abogen.web.service import Job, JobStatus, build_service, _JOB_LOGGER


def test_service_processes_job(tmp_path):
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    uploads.mkdir()
    outputs.mkdir()

    source = uploads / "sample.txt"
    payload = "hello world"
    source.write_text(payload, encoding="utf-8")

    runner_invocations: list[str] = []

    def runner(job):
        runner_invocations.append(job.id)
        job.add_log("processing")
        job.progress = 1.0
        job.processed_characters = job.total_characters or len(payload)
        job.result.audio_path = outputs / f"{job.id}.wav"

    service = build_service(
        runner=runner,
        output_root=outputs,
        uploads_root=uploads,
    )

    job = service.enqueue(
        original_filename="sample.txt",
        stored_path=source,
        language="a",
        voice="af_alloy",
        speed=1.0,
        use_gpu=False,
        subtitle_mode="Sentence",
        output_format="wav",
        save_mode="Save next to input file",
        output_folder=outputs,
        replace_single_newlines=False,
        subtitle_format="srt",
        total_characters=len(payload),
    )

    deadline = time.time() + 5
    while time.time() < deadline and job.status not in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
        time.sleep(0.05)

    service.shutdown()

    assert runner_invocations, "conversion runner was never called"
    assert job.status is JobStatus.COMPLETED
    assert job.progress == 1.0
    assert job.result.audio_path == outputs / f"{job.id}.wav"
    assert job.chunk_level == "paragraph"
    assert job.speaker_mode == "single"
    assert job.chunks == []
    assert not job.generate_epub3


def test_job_add_log_emits_to_stream(tmp_path):
    sample = tmp_path / "sample.txt"
    sample.write_text("payload", encoding="utf-8")

    job = Job(
        id="job-test",
        original_filename="sample.txt",
        stored_path=sample,
        language="a",
        voice="af_alloy",
        speed=1.0,
        use_gpu=False,
        subtitle_mode="Sentence",
        output_format="wav",
        save_mode="Save next to input file",
        output_folder=tmp_path,
        replace_single_newlines=False,
        subtitle_format="srt",
        created_at=time.time(),
    )

    captured_buffers = []
    for handler in list(_JOB_LOGGER.handlers):
        if not hasattr(handler, "setStream"):
            continue
        buffer = io.StringIO()
        original_stream = getattr(handler, "stream", None)
        handler.setStream(buffer)  # type: ignore[attr-defined]
        captured_buffers.append((handler, original_stream, buffer))

    assert captured_buffers, "Expected job logger to have stream handlers"

    try:
        job.add_log("Test log line", level="error")
        outputs = [buffer.getvalue() for _, _, buffer in captured_buffers]
    finally:
        for handler, original_stream, _ in captured_buffers:
            if hasattr(handler, "setStream"):
                handler.setStream(original_stream)  # type: ignore[attr-defined]

    assert any("Test log line" in output for output in outputs)
    assert job.logs[-1].message == "Test log line"


def test_retry_removes_failed_job(tmp_path):
    uploads = tmp_path / "uploads"
    outputs = tmp_path / "outputs"
    uploads.mkdir()
    outputs.mkdir()

    source = uploads / "sample.txt"
    source.write_text("hello", encoding="utf-8")

    def failing_runner(job):
        job.add_log("runner failing", level="error")
        raise RuntimeError("boom")

    service = build_service(
        runner=failing_runner,
        output_root=outputs,
        uploads_root=uploads,
    )

    try:
        job = service.enqueue(
            original_filename="sample.txt",
            stored_path=source,
            language="a",
            voice="af_alloy",
            speed=1.0,
            use_gpu=False,
            subtitle_mode="Sentence",
            output_format="wav",
            save_mode="Save next to input file",
            output_folder=outputs,
            replace_single_newlines=False,
            subtitle_format="srt",
            total_characters=len("hello"),
        )

        deadline = time.time() + 5
        while time.time() < deadline and job.status is not JobStatus.FAILED:
            time.sleep(0.05)

        assert job.status is JobStatus.FAILED

        new_job = service.retry(job.id)
        assert new_job is not None
        assert new_job.id != job.id

        job_ids = {entry.id for entry in service.list_jobs()}
        assert job.id not in job_ids
        assert new_job.id in job_ids
    finally:
        service.shutdown()