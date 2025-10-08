from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_DIALOGUE_VERBS = (
    "said",
    "asked",
    "replied",
    "whispered",
    "shouted",
    "cried",
    "muttered",
    "answered",
    "hissed",
    "called",
    "added",
    "continued",
    "insisted",
    "remarked",
    "yelled",
    "breathed",
    "murmured",
    "exclaimed",
    "explained",
    "noted",
)

_VERB_PATTERN = "(?:" + "|".join(_DIALOGUE_VERBS) + ")"
_NAME_FRAGMENT = r"[A-Z][A-Za-z'\-]+"
_NAME_PATTERN = rf"{_NAME_FRAGMENT}(?:\s+{_NAME_FRAGMENT})*"

_COLON_PATTERN = re.compile(rf"^\s*({_NAME_PATTERN})\s*:\s*(.+)$")
_NAME_BEFORE_VERB = re.compile(rf"({_NAME_PATTERN})\s+{_VERB_PATTERN}\b", re.IGNORECASE)
_VERB_BEFORE_NAME = re.compile(rf"{_VERB_PATTERN}\s+({_NAME_PATTERN})", re.IGNORECASE)
_PRONOUN_PATTERN = re.compile(r"\b(?:he|she|they)\b", re.IGNORECASE)
_QUOTE_PATTERN = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"')

_CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


@dataclass(slots=True)
class SpeakerGuess:
    speaker_id: str
    label: str
    count: int = 0
    confidence: str = "low"
    sample_quotes: List[str] = field(default_factory=list)
    suppressed: bool = False

    def register_occurrence(self, confidence: str, quote: Optional[str]) -> None:
        self.count += 1
        if _CONFIDENCE_RANK.get(confidence, 0) > _CONFIDENCE_RANK.get(self.confidence, 0):
            self.confidence = confidence
        if quote:
            normalized = quote.strip()
            if normalized and normalized not in self.sample_quotes:
                self.sample_quotes.append(normalized[:240])
                if len(self.sample_quotes) > 3:
                    self.sample_quotes = self.sample_quotes[:3]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.speaker_id,
            "label": self.label,
            "count": self.count,
            "confidence": self.confidence,
            "sample_quotes": list(self.sample_quotes),
            "suppressed": self.suppressed,
        }


@dataclass(slots=True)
class SpeakerAnalysis:
    assignments: Dict[str, str]
    speakers: Dict[str, SpeakerGuess]
    suppressed: List[str]
    narrator: str = "narrator"
    version: str = "1.0"
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "narrator": self.narrator,
            "assignments": dict(self.assignments),
            "speakers": {speaker_id: guess.as_dict() for speaker_id, guess in self.speakers.items()},
            "suppressed": list(self.suppressed),
            "stats": dict(self.stats),
        }


def analyze_speakers(
    chapters: Sequence[Dict[str, Any]] | Iterable[Dict[str, Any]],
    chunks: Sequence[Dict[str, Any]] | Iterable[Dict[str, Any]],
    *,
    threshold: int = 3,
    max_speakers: int = 8,
) -> SpeakerAnalysis:
    narrator_id = "narrator"
    speaker_guesses: Dict[str, SpeakerGuess] = {
        narrator_id: SpeakerGuess(speaker_id=narrator_id, label="Narrator", confidence="low")
    }
    label_index: Dict[str, str] = {"Narrator": narrator_id}
    assignments: Dict[str, str] = {}
    suppressed: List[str] = []

    ordered_chunks = sorted(
        (dict(chunk) for chunk in chunks),
        key=lambda entry: (
            _safe_int(entry.get("chapter_index")),
            _safe_int(entry.get("chunk_index")),
        ),
    )
    last_explicit: Optional[str] = None
    explicit_assignments = 0
    unique_speakers: set[str] = set()

    for chunk in ordered_chunks:
        chunk_id = str(chunk.get("id") or "")
        text = str(chunk.get("text") or "")
        speaker_id, confidence, quote = _infer_chunk_speaker(text, last_explicit)
        if speaker_id is None:
            speaker_id = last_explicit or narrator_id
            confidence = "medium" if last_explicit else "low"
            quote = quote or _extract_quote(text)
        if speaker_id != narrator_id:
            last_explicit = speaker_id
            explicit_assignments += 1
        assignments[chunk_id] = speaker_id
        unique_speakers.add(speaker_id)

        label = _normalize_label(speaker_id)
        record_id = label_index.get(label)
        if record_id is None:
            record_id = _dedupe_slug(_slugify(label), speaker_guesses)
            label_index[label] = record_id
            speaker_guesses[record_id] = SpeakerGuess(speaker_id=record_id, label=label)
        guess = speaker_guesses[record_id]
        guess.register_occurrence(confidence, quote)
        if record_id != speaker_id:
            # Maintain mapping to canonical ID in assignments.
            assignments[chunk_id] = record_id
            if speaker_id == last_explicit:
                last_explicit = record_id

    active_speakers = [sid for sid in speaker_guesses if sid != narrator_id]
    # Apply minimum occurrence threshold.
    for speaker_id in list(active_speakers):
        guess = speaker_guesses[speaker_id]
        if guess.count < max(1, threshold):
            guess.suppressed = True
            suppressed.append(speaker_id)
            _reassign(assignments, speaker_id, narrator_id)
            active_speakers.remove(speaker_id)

    # Apply maximum active speaker cap.
    if max_speakers and len(active_speakers) > max_speakers:
        active_speakers.sort(key=lambda sid: (-speaker_guesses[sid].count, sid))
        for speaker_id in active_speakers[max_speakers:]:
            guess = speaker_guesses[speaker_id]
            guess.suppressed = True
            suppressed.append(speaker_id)
            _reassign(assignments, speaker_id, narrator_id)
        active_speakers = active_speakers[:max_speakers]

    narrator_guess = speaker_guesses[narrator_id]
    narrator_guess.count = sum(1 for value in assignments.values() if value == narrator_id)
    narrator_guess.confidence = "low"

    stats = {
        "total_chunks": len(ordered_chunks),
        "explicit_chunks": explicit_assignments,
        "active_speakers": len(active_speakers),
        "unique_speakers": len(unique_speakers),
        "suppressed": len(suppressed),
    }

    return SpeakerAnalysis(
        assignments=assignments,
        speakers=speaker_guesses,
        suppressed=suppressed,
        narrator=narrator_id,
        stats=stats,
    )


def _infer_chunk_speaker(text: str, last_explicit: Optional[str]) -> Tuple[Optional[str], str, Optional[str]]:
    normalized = text.strip()
    if not normalized:
        return None, "low", None

    colon_match = _COLON_PATTERN.match(normalized)
    if colon_match:
        raw_label = colon_match.group(1)
        quote = colon_match.group(2).strip()
        return raw_label, "high", quote

    quote = _extract_quote(normalized)
    if not quote:
        return None, "low", None

    before, after = _split_around_quote(normalized, quote)

    candidate = _match_name_near_quote(before, after)
    if candidate:
        return candidate, "high", quote

    if last_explicit:
        pronoun_after = _PRONOUN_PATTERN.search(after)
        pronoun_before = _PRONOUN_PATTERN.search(before)
        if pronoun_after or pronoun_before:
            return last_explicit, "medium", quote

    return None, "low", quote


def _split_around_quote(text: str, quote: str) -> Tuple[str, str]:
    quote_index = text.find(quote)
    if quote_index == -1:
        return text, ""
    before = text[:quote_index]
    after = text[quote_index + len(quote) :]
    return before, after


def _match_name_near_quote(before: str, after: str) -> Optional[str]:
    trailing = before[-120:]
    leading = after[:120]

    match = _NAME_BEFORE_VERB.search(trailing)
    if match:
        name = match.group(1)
        if _looks_like_name(name):
            return name

    match = re.search(rf"({_NAME_PATTERN})\s*,?\s*{_VERB_PATTERN}", leading, flags=re.IGNORECASE)
    if match:
        name = match.group(1)
        if _looks_like_name(name):
            return name

    match = _VERB_BEFORE_NAME.search(leading)
    if match:
        name = match.group(1)
        if _looks_like_name(name):
            return name

    return None


def _looks_like_name(value: str) -> bool:
    parts = value.strip().split()
    if not parts:
        return False
    return all(part[0].isupper() for part in parts)


def _extract_quote(text: str) -> Optional[str]:
    match = _QUOTE_PATTERN.search(text)
    if not match:
        return None
    return match.group(0)


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug or "speaker"


def _dedupe_slug(slug: str, existing: Dict[str, SpeakerGuess]) -> str:
    candidate = slug
    index = 2
    while candidate in existing:
        candidate = f"{slug}_{index}"
        index += 1
    return candidate


def _normalize_label(label: str) -> str:
    words = re.split(r"\s+", label.strip())
    return " ".join(word.capitalize() for word in words if word)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _reassign(assignments: Dict[str, str], old: str, new: str) -> None:
    for key, value in list(assignments.items()):
        if value == old:
            assignments[key] = new