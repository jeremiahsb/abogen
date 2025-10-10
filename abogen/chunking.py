from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Literal, Optional

import re

from abogen.kokoro_text_normalization import ApostropheConfig, normalize_for_pipeline

ChunkLevel = Literal["paragraph", "sentence"]

_SENTENCE_SPLIT_REGEX = re.compile(r"(?<!\b[A-Z])[.!?][\s\n]+")
_WHITESPACE_REGEX = re.compile(r"\s+")
_PARAGRAPH_SPLIT_REGEX = re.compile(r"(?:\r?\n){2,}")
_ABBREVIATION_END_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Rev|Sr|Jr|St|Gen|Lt|Col|Sgt|Capt|Adm|Cmdr|vs|etc)\.$",
    re.IGNORECASE,
)

_PIPELINE_APOSTROPHE_CONFIG = ApostropheConfig()


@dataclass(frozen=True)
class Chunk:
    id: str
    chapter_index: int
    chunk_index: int
    level: ChunkLevel
    text: str
    speaker_id: str = "narrator"
    voice: Optional[str] = None
    voice_profile: Optional[str] = None
    voice_formula: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "chapter_index": self.chapter_index,
            "chunk_index": self.chunk_index,
            "level": self.level,
            "text": self.text,
            "speaker_id": self.speaker_id,
            "voice": self.voice,
            "voice_profile": self.voice_profile,
            "voice_formula": self.voice_formula,
        }


def _iter_paragraphs(text: str) -> Iterator[str]:
    for raw_segment in _PARAGRAPH_SPLIT_REGEX.split(text.strip()):
        normalized = raw_segment.strip()
        if normalized:
            yield normalized


def _iter_sentences(paragraph: str) -> Iterator[str]:
    if not paragraph:
        return
    start = 0
    for match in _SENTENCE_SPLIT_REGEX.finditer(paragraph):
        end = match.end()
        candidate = paragraph[start:end].strip()
        if candidate:
            yield candidate
        start = match.end()
    tail = paragraph[start:].strip()
    if tail:
        yield tail


def _normalize_whitespace(value: str) -> str:
    return _WHITESPACE_REGEX.sub(" ", value).strip()


def _normalize_chunk_text(value: str) -> str:
    normalized = normalize_for_pipeline(value, config=_PIPELINE_APOSTROPHE_CONFIG)
    return _normalize_whitespace(normalized)


def _split_sentences(paragraph: str) -> List[str]:
    sentences = list(_iter_sentences(paragraph))
    if not sentences:
        return []

    merged: List[str] = []
    buffer: List[str] = []

    for sentence in sentences:
        if buffer:
            buffer.append(sentence)
        else:
            buffer = [sentence]

        if _ABBREVIATION_END_RE.search(sentence.rstrip()):
            continue

        merged.append(" ".join(buffer))
        buffer = []

    if buffer:
        merged.append(" ".join(buffer))

    return merged


def chunk_text(
    *,
    chapter_index: int,
    chapter_title: str,
    text: str,
    level: ChunkLevel,
    speaker_id: str = "narrator",
    voice: Optional[str] = None,
    voice_profile: Optional[str] = None,
    voice_formula: Optional[str] = None,
    chunk_prefix: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Split text into ordered chunk dictionaries."""

    prefix = chunk_prefix or f"chap{chapter_index:04d}"
    chunks: List[Dict[str, object]] = []

    if level == "paragraph":
        paragraphs = list(_iter_paragraphs(text)) or [text.strip()]
        for para_index, paragraph in enumerate(paragraphs):
            normalized = _normalize_whitespace(paragraph)
            if not normalized:
                continue
            chunk_id = f"{prefix}_p{para_index:04d}"
            payload = Chunk(
                id=chunk_id,
                chapter_index=chapter_index,
                chunk_index=len(chunks),
                level=level,
                text=normalized,
                speaker_id=speaker_id,
                voice=voice,
                voice_profile=voice_profile,
                voice_formula=voice_formula,
            ).as_dict()
            payload["normalized_text"] = _normalize_chunk_text(paragraph)
            chunks.append(payload)
        return chunks

    # Sentence level – flatten paragraphs into individual sentences
    sentence_index = 0
    for para_index, paragraph in enumerate(list(_iter_paragraphs(text)) or [text.strip()]):
        normalized_para = _normalize_whitespace(paragraph)
        if not normalized_para:
            continue
        sentences = _split_sentences(normalized_para) or [normalized_para]
        for sent_local_index, sentence in enumerate(sentences):
            normalized_sentence = _normalize_whitespace(sentence)
            if not normalized_sentence:
                continue
            chunk_id = f"{prefix}_p{para_index:04d}_s{sent_local_index:04d}"
            payload = Chunk(
                id=chunk_id,
                chapter_index=chapter_index,
                chunk_index=sentence_index,
                level=level,
                text=normalized_sentence,
                speaker_id=speaker_id,
                voice=voice,
                voice_profile=voice_profile,
                voice_formula=voice_formula,
            ).as_dict()
            payload["normalized_text"] = _normalize_chunk_text(sentence)
            chunks.append(payload)
            sentence_index += 1

    return chunks


def build_chunks_for_chapters(
    chapters: Iterable[Dict[str, object]],
    *,
    level: ChunkLevel,
    speaker_id: str = "narrator",
) -> List[Dict[str, object]]:
    """Generate chunk dictionaries for a sequence of chapter payloads."""
    all_chunks: List[Dict[str, object]] = []
    for chapter_index, entry in enumerate(chapters):
        if not isinstance(entry, dict):  # defensive
            continue
        text = str(entry.get("text", "") or "").strip()
        if not text:
            continue
        voice = entry.get("voice")
        voice_profile = entry.get("voice_profile")
        voice_formula = entry.get("voice_formula")
        prefix = entry.get("id") or f"chap{chapter_index:04d}"
        chapter_chunks = chunk_text(
            chapter_index=chapter_index,
            chapter_title=str(entry.get("title") or f"Chapter {chapter_index + 1}"),
            text=text,
            level=level,
            speaker_id=speaker_id,
            voice=str(voice) if voice else None,
            voice_profile=str(voice_profile) if voice_profile else None,
            voice_formula=str(voice_formula) if voice_formula else None,
            chunk_prefix=str(prefix),
        )
        all_chunks.extend(chapter_chunks)
    return all_chunks