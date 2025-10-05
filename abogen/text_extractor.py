from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import List, Sequence

import ebooklib
import fitz
import markdown
from bs4 import BeautifulSoup
from ebooklib import epub

from .utils import clean_text, detect_encoding


@dataclass
class ExtractedChapter:
    title: str
    text: str

    @property
    def characters(self) -> int:
        return len(self.text)


@dataclass
class ExtractionResult:
    chapters: List[ExtractedChapter]
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def combined_text(self) -> str:
        return "\n\n".join(chapter.text for chapter in self.chapters)

    @property
    def total_characters(self) -> int:
        return sum(chapter.characters for chapter in self.chapters)


def extract_from_path(path: Path) -> ExtractionResult:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return _extract_plaintext(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    if suffix in {".md", ".markdown"}:
        return _extract_markdown(path)
    if suffix == ".epub":
        return _extract_epub(path)
    raise ValueError(f"Unsupported input type: {suffix}")


def _extract_plaintext(path: Path) -> ExtractionResult:
    encoding = detect_encoding(str(path))
    raw = path.read_text(encoding=encoding, errors="replace")
    return _extract_from_string(raw, default_title=path.stem)


METADATA_PATTERN = re.compile(r"<<METADATA_([A-Z_]+):(.*?)>>", re.DOTALL)
CHAPTER_PATTERN = re.compile(r"<<CHAPTER_MARKER:(.*?)>>", re.IGNORECASE)


def _extract_from_string(raw: str, default_title: str) -> ExtractionResult:
    metadata, body = _strip_metadata(raw)
    chapters = _split_chapters(body, default_title)
    if not chapters:
        chapters = [ExtractedChapter(title=default_title, text="")]
    return ExtractionResult(chapters=chapters, metadata=metadata)


def _strip_metadata(content: str) -> tuple[dict[str, str], str]:
    metadata: dict[str, str] = {}

    def _replacer(match: re.Match) -> str:
        key = match.group(1).strip().upper()
        value = match.group(2).strip()
        if value:
            metadata[key] = value
        return ""

    stripped = METADATA_PATTERN.sub(_replacer, content)
    return metadata, stripped


def _split_chapters(content: str, default_title: str) -> List[ExtractedChapter]:
    matches = list(CHAPTER_PATTERN.finditer(content))
    if not matches:
        cleaned = clean_text(content)
        return [ExtractedChapter(title=default_title, text=cleaned)]

    chapters: List[ExtractedChapter] = []
    last_index = 0
    current_title = default_title

    for match in matches:
        segment = content[last_index:match.start()]
        if segment.strip():
            chapters.append(ExtractedChapter(title=current_title, text=clean_text(segment)))
        current_title = match.group(1).strip() or default_title
        last_index = match.end()

    tail = content[last_index:]
    if tail.strip():
        chapters.append(ExtractedChapter(title=current_title, text=clean_text(tail)))

    return chapters


def _extract_pdf(path: Path) -> ExtractionResult:
    document = fitz.open(str(path))
    chapters: List[ExtractedChapter] = []
    for index, page in enumerate(document):
        text = clean_text(page.get_text())
        if not text:
            continue
        title = f"Page {index + 1}"
        chapters.append(ExtractedChapter(title=title, text=text))
    if not chapters:
        chapters.append(ExtractedChapter(title=path.stem, text=""))
    return ExtractionResult(chapters)


def _extract_markdown(path: Path) -> ExtractionResult:
    encoding = detect_encoding(str(path))
    raw = path.read_text(encoding=encoding, errors="replace")
    html = markdown.markdown(raw, extensions=["toc", "fenced_code"])
    soup = BeautifulSoup(html, "html.parser")
    headings = soup.find_all([f"h{i}" for i in range(1, 7)])
    chapters: List[ExtractedChapter] = []
    if headings:
        for heading in headings:
            sibling_text = _collect_heading_text(heading)
            text = clean_text(sibling_text)
            if text:
                chapters.append(ExtractedChapter(title=heading.get_text(strip=True), text=text))
    if not chapters:
        chapters.append(ExtractedChapter(title=path.stem, text=clean_text(raw)))
    return ExtractionResult(chapters)


def _collect_heading_text(node) -> str:
    texts: List[str] = []
    for sibling in node.next_siblings:
        if getattr(sibling, "name", None) and sibling.name.startswith("h"):
            break
        text = getattr(sibling, "get_text", lambda **_: "")()
        if text:
            texts.append(text)
    return "\n".join(texts)


def _extract_epub(path: Path) -> ExtractionResult:
    book = epub.read_epub(str(path))
    chapters: List[ExtractedChapter] = []
    spine_docs: Sequence[str] = [item[0] for item in book.spine]
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        name = item.get_name()
        if name not in spine_docs:
            continue
        html_bytes = item.get_content()
        soup = BeautifulSoup(html_bytes, "html.parser")
        for ol in soup.find_all("ol"):
            start = int(ol.get("start", 1))
            for idx, li in enumerate(ol.find_all("li", recursive=False)):
                number = f"{start + idx}. "
                if li.string:
                    li.string.replace_with(number + li.string)
                else:
                    li.insert(0, number)
        text = clean_text(soup.get_text())
        if not text:
            continue
        title = _resolve_epub_title(soup, name)
        chapters.append(ExtractedChapter(title=title, text=text))
    if not chapters:
        chapters.append(ExtractedChapter(title=path.stem, text=""))
    return ExtractionResult(chapters)


def _resolve_epub_title(soup: BeautifulSoup, fallback: str) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    for heading_tag in ("h1", "h2", "h3"):
        heading = soup.find(heading_tag)
        if heading and heading.get_text(strip=True):
            return heading.get_text(strip=True)
    return fallback
