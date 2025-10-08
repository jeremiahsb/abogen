from pathlib import Path

from abogen.text_extractor import extract_from_path
from abogen.utils import calculate_text_length


ASSET = Path("test_assets/alexandre-dumas_the-count-of-monte-cristo_chapman-and-hall.epub")


def test_epub_character_counts_align_with_calculated_total():
    result = extract_from_path(ASSET)

    combined_total = calculate_text_length(result.combined_text)
    chapter_total = sum(chapter.characters for chapter in result.chapters)

    assert result.total_characters == combined_total == chapter_total


def test_epub_metadata_composer_matches_artist():
    result = extract_from_path(ASSET)

    composer = result.metadata.get("composer") or result.metadata.get("COMPOSER")
    artist = result.metadata.get("artist") or result.metadata.get("ARTIST")

    assert composer
    assert composer == artist
    assert composer != "Narrator"
