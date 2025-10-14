from abogen.web.conversion_runner import (
    _format_spoken_chapter_title,
    _headings_equivalent,
    _strip_duplicate_heading_line,
)


def test_format_spoken_chapter_title_adds_prefix() -> None:
    assert _format_spoken_chapter_title("1: A Tale", 1, True) == "Chapter 1: A Tale"


def test_format_spoken_chapter_title_respects_existing_prefix() -> None:
    assert _format_spoken_chapter_title("Chapter 2: Story", 2, True) == "Chapter 2: Story"


def test_format_spoken_chapter_title_handles_empty_title() -> None:
    assert _format_spoken_chapter_title("", 4, True) == "Chapter 4"


def test_headings_equivalent_ignores_case_and_prefix() -> None:
    assert _headings_equivalent("1: The House", "Chapter 1: The House")


def test_strip_duplicate_heading_line_removes_first_match() -> None:
    text, removed = _strip_duplicate_heading_line("Chapter 3: Intro\nBody text", "Chapter 3: Intro")
    assert removed is True
    assert text.strip() == "Body text"
