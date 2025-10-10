from __future__ import annotations

from abogen.kokoro_text_normalization import normalize_roman_numeral_titles
from abogen.web.conversion_runner import _normalize_for_pipeline


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
