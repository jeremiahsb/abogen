from __future__ import annotations

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
