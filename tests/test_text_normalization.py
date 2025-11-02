from __future__ import annotations

import pytest

from abogen.kokoro_text_normalization import normalize_roman_numeral_titles
from abogen.web.conversion_runner import _normalize_for_pipeline
from abogen.spacy_contraction_resolver import resolve_ambiguous_contractions


SPACY_RESOLVER_AVAILABLE = bool(resolve_ambiguous_contractions("It's been a long time."))


def test_title_abbreviations_are_expanded():
    text = "Dr. Watson met Mr. Holmes and Ms. Hudson."
    normalized = _normalize_for_pipeline(text)
    assert "Doctor" in normalized
    assert "Mister" in normalized
    assert "Miz" in normalized


def test_suffix_abbreviations_are_expanded_with_case_preserved():
    text = "John Doe Jr. spoke to JANE DOE SR. about the estate."
    normalized = _normalize_for_pipeline(text)
    assert "John Doe Junior" in normalized
    assert "JANE DOE SENIOR" in normalized


def test_missing_terminal_punctuation_is_added():
    normalized = _normalize_for_pipeline("Chapter 1")
    assert normalized.endswith(".")


def test_terminal_punctuation_respects_closing_quotes():
    normalized = _normalize_for_pipeline('"Chapter 1"')
    compact = normalized.replace(" ", "")
    assert compact.endswith('."')


def test_normalization_preserves_spacing_around_quotes_and_hyphen():
    sample = "“Still,” said Château-Renaud, “Dr. d’Avrigny, who attends my mother, declares he is in despair about it."
    normalized = _normalize_for_pipeline(sample)

    assert normalized.startswith(
        "“Still,” said Château-Renaud, “Doctor d'Avrigny, who attends my mother, declares he is in despair about it."
    )
    assert "  " not in normalized
    assert "Château-Renaud" in normalized
    assert "Doctor d'Avrigny" in normalized


def test_normalize_roman_titles_converts_when_majority() -> None:
    titles = ["I: Opening", "II: Rising Action", "III: Climax"]
    normalized = normalize_roman_numeral_titles(titles)

    assert normalized == ["1: Opening", "2: Rising Action", "3: Climax"]


def test_normalize_roman_titles_skips_when_not_majority() -> None:
    titles = ["Preface", "I: Opening", "Acknowledgements"]
    normalized = normalize_roman_numeral_titles(titles)

    assert normalized == titles


def test_normalize_roman_titles_preserves_separators() -> None:
    titles = ["  IV.  The Trial", "V - The Verdict", "VI\nAftermath"]
    normalized = normalize_roman_numeral_titles(titles)

    assert normalized[0] == "  4.  The Trial"
    assert normalized[1] == "5 - The Verdict"
    assert normalized[2].startswith("6\nAftermath")


def test_grouped_numbers_are_spelled_out() -> None:
    normalized = _normalize_for_pipeline("The vault holds 35,000 credits")
    assert "thirty-five thousand" in normalized.lower()


def test_numeric_ranges_are_spoken_with_to() -> None:
    normalized = _normalize_for_pipeline("Chapters 1-3")
    assert "one to three" in normalized.lower()


def test_simple_fractions_are_spoken() -> None:
    normalized = _normalize_for_pipeline("Add 1/2 cup of sugar")
    assert "one half" in normalized.lower()


def test_contractions_can_be_kept_when_override_disabled() -> None:
    normalized = _normalize_for_pipeline(
        "It's a good day.",
        normalization_overrides={"normalization_apostrophes_contractions": False},
    )
    assert "It's" in normalized


def test_sibilant_possessives_remain_when_marking_disabled() -> None:
    normalized = _normalize_for_pipeline(
        "The boss's chair wobbled.",
        normalization_overrides={"normalization_apostrophes_sibilant_possessives": False},
    )
    assert "boss's" in normalized
    assert "boss iz" not in normalized.lower()


def test_decades_can_skip_expansion_when_disabled() -> None:
    normalized = _normalize_for_pipeline(
        "Classic hits from the '90s filled the hall.",
        normalization_overrides={"normalization_apostrophes_decades": False},
    )
    assert "'90s" in normalized


@pytest.mark.skipif(not SPACY_RESOLVER_AVAILABLE, reason="spaCy model unavailable")
def test_spacy_disambiguates_it_has_from_context() -> None:
    normalized = _normalize_for_pipeline("It's been a long time.")
    assert "It has been a long time." == normalized


@pytest.mark.skipif(not SPACY_RESOLVER_AVAILABLE, reason="spaCy model unavailable")
def test_spacy_disambiguates_it_is_from_context() -> None:
    normalized = _normalize_for_pipeline("It's cold outside.")
    assert "It is cold outside." == normalized


@pytest.mark.skipif(not SPACY_RESOLVER_AVAILABLE, reason="spaCy model unavailable")
def test_spacy_disambiguates_she_had() -> None:
    normalized = _normalize_for_pipeline("She'd left before dawn.")
    assert "She had left before dawn." == normalized


@pytest.mark.skipif(not SPACY_RESOLVER_AVAILABLE, reason="spaCy model unavailable")
def test_spacy_disambiguates_she_would() -> None:
    normalized = _normalize_for_pipeline("She'd go if invited.")
    assert "She would go if invited." == normalized


@pytest.mark.skipif(not SPACY_RESOLVER_AVAILABLE, reason="spaCy model unavailable")
def test_sample_sentence_handles_complex_contractions() -> None:
    sample = "I've heard the captain'll arrive by dusk, but they'd said the same yesterday."
    normalized = _normalize_for_pipeline(sample)
    assert (
        "I have heard the captain will arrive by dusk, but they had said the same yesterday." == normalized
    )


def test_modal_will_contractions_can_be_disabled() -> None:
    sample = "The captain'll arrive at dawn."
    normalized = _normalize_for_pipeline(
        sample,
        normalization_overrides={"normalization_contraction_modal_will": False},
    )
    assert "captain'll" in normalized
