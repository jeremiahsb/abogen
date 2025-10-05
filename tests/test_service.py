from __future__ import annotations

import time
from abogen.web.service import JobStatus, build_service


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