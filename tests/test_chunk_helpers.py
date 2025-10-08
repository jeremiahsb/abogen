from __future__ import annotations

from types import SimpleNamespace

from abogen.web.conversion_runner import _chunk_voice_spec, _group_chunks_by_chapter


def test_group_chunks_by_chapter_orders_and_groups() -> None:
    chunks = [
        {"chapter_index": "0", "chunk_index": "5", "text": "tail"},
        {"chapter_index": 0, "chunk_index": 1, "text": "body"},
        {"chapter_index": 1, "chunk_index": 0, "text": "next"},
    ]

    grouped = _group_chunks_by_chapter(chunks)

    assert [entry["text"] for entry in grouped[0]] == ["body", "tail"]
    assert grouped[1][0]["text"] == "next"


def test_chunk_voice_spec_prefers_chunk_overrides() -> None:
    job = SimpleNamespace(voice="base_voice", speakers={})
    chunk = {"voice": "override_voice", "speaker_id": "narrator"}

    assert _chunk_voice_spec(job, chunk, "fallback") == "override_voice"


def test_chunk_voice_spec_falls_back_to_speaker_voice() -> None:
    job = SimpleNamespace(voice="base_voice", speakers={"narrator": {"voice": "speaker_voice"}})
    chunk = {"speaker_id": "narrator"}

    assert _chunk_voice_spec(job, chunk, "fallback") == "speaker_voice"


def test_chunk_voice_spec_uses_fallback_when_no_overrides() -> None:
    job = SimpleNamespace(voice="base_voice", speakers={})
    chunk = {"speaker_id": "unknown"}

    assert _chunk_voice_spec(job, chunk, "fallback") == "fallback"
