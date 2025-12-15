from __future__ import annotations

import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence


MARKER_PREFIX = "[[ABOGEN-DBG:"
MARKER_SUFFIX = "]]"


@dataclass(frozen=True)
class DebugTTSSample:
    code: str
    label: str
    text: str


DEBUG_TTS_SAMPLES: Sequence[DebugTTSSample] = (
    DebugTTSSample(
        code="APOS_001",
        label="Apostrophes & contractions",
        text="It's a beautiful day, isn't it? Let's see what we'll do.",
    ),
    DebugTTSSample(
        code="POS_001",
        label="Plural possessives",
        text="The dogs' bowls were empty, but the boss's office was quiet.",
    ),
    DebugTTSSample(
        code="NUM_001",
        label="Grouped numbers",
        text="There are 1,234 apples, 56 oranges, and 7.89 liters of juice.",
    ),
    DebugTTSSample(
        code="YEAR_001",
        label="Years and decades",
        text="In 1999, people said the '90s were over.",
    ),
    DebugTTSSample(
        code="DATE_001",
        label="ISO dates",
        text="On 2023-01-01, we celebrated the new year.",
    ),
    DebugTTSSample(
        code="CUR_001",
        label="Currency symbols",
        text="The price is $10.50, but it was £8.00 yesterday.",
    ),
    DebugTTSSample(
        code="TITLE_001",
        label="Titles and abbreviations",
        text="Dr. Smith lives on Elm St. near the U.S. border.",
    ),
    DebugTTSSample(
        code="PUNC_001",
        label="Terminal punctuation",
        text="This sentence ends without punctuation",
    ),
    DebugTTSSample(
        code="QUOTE_001",
        label="ALL CAPS inside quotes",
        text='He shouted, "THIS IS IMPORTANT!" and then whispered, "ok."',
    ),
    DebugTTSSample(
        code="FOOT_001",
        label="Footnote indicators",
        text="This is a sentence with a footnote[1] and another[12].",
    ),
)


def marker_for(code: str) -> str:
    return f"{MARKER_PREFIX}{code}{MARKER_SUFFIX}"


def build_debug_epub(dest_path: Path, *, title: str = "abogen debug samples") -> Path:
    """Create a tiny EPUB containing all debug samples.

    The text includes stable marker codes so developers can report failures
    precisely.
    """

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    chapter_lines: List[str] = [
        "<?xml version=\"1.0\" encoding=\"utf-8\"?>",
        "<!DOCTYPE html>",
        "<html xmlns=\"http://www.w3.org/1999/xhtml\">",
        "<head>",
        f"  <title>{title}</title>",
        "  <meta charset=\"utf-8\" />",
        "</head>",
        "<body>",
        f"  <h1>{title}</h1>",
        "  <p>Each paragraph begins with a stable debug code marker.</p>",
    ]

    for sample in DEBUG_TTS_SAMPLES:
        safe_label = sample.label.replace("&", "and")
        chapter_lines.append(f"  <h2>{safe_label}</h2>")
        chapter_lines.append(
            "  <p><strong>" + marker_for(sample.code) + "</strong> " + sample.text + "</p>"
        )

    chapter_lines += ["</body>", "</html>"]
    chapter_xhtml = "\n".join(chapter_lines)

    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    content_opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">abogen-debug-samples</dc:identifier>
    <dc:title>abogen debug samples</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml" />
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />
  </manifest>
  <spine>
    <itemref idref="chapter" />
  </spine>
</package>
"""

    nav_xhtml = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>Navigation</title>
  <meta charset="utf-8" />
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h2>Table of Contents</h2>
    <ol>
      <li><a href="chapter.xhtml">Debug samples</a></li>
    </ol>
  </nav>
</body>
</html>
"""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "mimetype").write_text("application/epub+zip", encoding="utf-8")
        meta_inf = tmp_path / "META-INF"
        meta_inf.mkdir(parents=True, exist_ok=True)
        (meta_inf / "container.xml").write_text(container_xml, encoding="utf-8")
        oebps = tmp_path / "OEBPS"
        oebps.mkdir(parents=True, exist_ok=True)
        (oebps / "content.opf").write_text(content_opf, encoding="utf-8")
        (oebps / "chapter.xhtml").write_text(chapter_xhtml, encoding="utf-8")
        (oebps / "nav.xhtml").write_text(nav_xhtml, encoding="utf-8")

        # Per EPUB spec: mimetype must be the first entry and stored (no compression).
        with zipfile.ZipFile(dest_path, "w") as zf:
            zf.write(tmp_path / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
            for source in (meta_inf / "container.xml", oebps / "content.opf", oebps / "chapter.xhtml", oebps / "nav.xhtml"):
                arcname = str(source.relative_to(tmp_path)).replace("\\", "/")
                zf.write(source, arcname, compress_type=zipfile.ZIP_DEFLATED)

    return dest_path


def iter_expected_codes() -> Iterable[str]:
    for sample in DEBUG_TTS_SAMPLES:
        yield sample.code
