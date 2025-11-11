from __future__ import annotations

import dataclasses
import html
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx


ATOM_NS = "http://www.w3.org/2005/Atom"
OPDS_NS = "http://opds-spec.org/2010/catalog"
DC_NS = "http://purl.org/dc/terms/"
CALIBRE_CATALOG_NS = "http://calibre.kovidgoyal.net/2009/catalog"
CALIBRE_METADATA_NS = "http://calibre.kovidgoyal.net/2009/metadata"
NS = {
    "atom": ATOM_NS,
    "opds": OPDS_NS,
    "dc": DC_NS,
    "calibre": CALIBRE_CATALOG_NS,
    "calibre_md": CALIBRE_METADATA_NS,
}


_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_SERIES_PREFIX_RE = re.compile(r"^\s*(series|books?)\s*[:\-]\s*", re.IGNORECASE)
_SERIES_NUMBER_BRACKET_RE = re.compile(r"[\[(]\s*(?:book\s*)?(\d+(?:\.\d+)?)\s*[\])]", re.IGNORECASE)
_SERIES_NUMBER_HASH_RE = re.compile(r"#\s*(\d+(?:\.\d+)?)")
_SERIES_NUMBER_BOOK_RE = re.compile(r"\bbook\s+(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_SERIES_LINE_TEXT_RE = re.compile(r"^\s*series\s*[:\-]\s*(.+)$", re.IGNORECASE)
_SUMMARY_METADATA_LINE_RE = re.compile(r"^([A-Z][A-Z0-9&/\- +'\u2019]{1,40})\s*[:\-]\s*(.+)$")
_EPUB_MIME_TYPES = {
    "application/epub+zip",
    "application/zip",
    "application/x-zip",
    "application/x-zip-compressed",
}


class CalibreOPDSError(RuntimeError):
    """Raised when the Calibre OPDS client encounters an unrecoverable error."""


@dataclass
class OPDSLink:
    href: str
    rel: Optional[str] = None
    type: Optional[str] = None
    title: Optional[str] = None

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "href": self.href,
            "rel": self.rel,
            "type": self.type,
            "title": self.title,
        }


@dataclass
class OPDSEntry:
    id: str
    title: str
    position: Optional[int] = None
    authors: List[str] = field(default_factory=list)
    updated: Optional[str] = None
    published: Optional[str] = None
    summary: Optional[str] = None
    download: Optional[OPDSLink] = None
    alternate: Optional[OPDSLink] = None
    thumbnail: Optional[OPDSLink] = None
    links: List[OPDSLink] = field(default_factory=list)
    series: Optional[str] = None
    series_index: Optional[float] = None
    tags: List[str] = field(default_factory=list)
    rating: Optional[float] = None
    rating_max: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "position": self.position,
            "authors": list(self.authors),
            "updated": self.updated,
            "published": self.published,
            "summary": self.summary,
            "download": self.download.to_dict() if self.download else None,
            "alternate": self.alternate.to_dict() if self.alternate else None,
            "thumbnail": self.thumbnail.to_dict() if self.thumbnail else None,
            "links": [link.to_dict() for link in self.links],
            "series": self.series,
            "series_index": self.series_index,
            "tags": list(self.tags),
            "rating": self.rating,
            "rating_max": self.rating_max,
        }


@dataclass
class OPDSFeed:
    id: Optional[str]
    title: Optional[str]
    entries: List[OPDSEntry]
    links: Dict[str, OPDSLink] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "entries": [entry.to_dict() for entry in self.entries],
            "links": {key: link.to_dict() for key, link in self.links.items()},
        }


@dataclass
class DownloadedResource:
    filename: str
    mime_type: str
    content: bytes


class CalibreOPDSClient:
    """Client for interacting with a Calibre-Web OPDS catalog."""

    def __init__(
        self,
        base_url: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 15.0,
        verify: bool = True,
    ) -> None:
        if not base_url:
            raise ValueError("Calibre OPDS base URL is required")
        normalized = base_url.strip()
        if not normalized:
            raise ValueError("Calibre OPDS base URL is required")
        if not normalized.endswith("/"):
            normalized = f"{normalized}/"
        self._base_url = normalized
        self._auth = None
        if username:
            self._auth = httpx.BasicAuth(username, password or "")
        self._timeout = timeout
        self._verify = verify
        self._headers = {
            "User-Agent": "abogen-calibre-opds/1.0",
            "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    @staticmethod
    def _strip_html(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        cleaned = _TAG_STRIP_RE.sub("", value)
        return html.unescape(cleaned).strip() or None

    def _make_url(self, href: Optional[str]) -> str:
        if not href:
            return self._base_url
        href = href.strip()
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if href.startswith("/") or href.startswith("?") or href.startswith("#"):
            return urljoin(self._base_url, href)
        if href.startswith("./") or href.startswith("../"):
            return urljoin(self._base_url, href)
        # Ensure relative paths like "search" keep the catalog prefix
        return urljoin(self._base_url, f"./{href}")

    def _open_client(self) -> httpx.Client:
        return httpx.Client(
            auth=self._auth,
            headers=dict(self._headers),
            timeout=self._timeout,
            verify=self._verify,
        )

    def fetch_feed(self, href: Optional[str] = None, *, params: Optional[Mapping[str, Any]] = None) -> OPDSFeed:
        target = self._make_url(href)
        try:
            with self._open_client() as client:
                response = client.get(target, params=params, follow_redirects=True)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - thin wrapper
            raise CalibreOPDSError(f"Calibre OPDS request failed: {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:  # pragma: no cover - thin wrapper
            raise CalibreOPDSError(f"Calibre OPDS request failed: {exc}") from exc

        return self._parse_feed(response.text, base_url=target)

    def search(self, query: str) -> OPDSFeed:
        cleaned = (query or "").strip()
        if not cleaned:
            return self.fetch_feed()
        candidates = [
            ("search", {"query": cleaned}),
            ("search", {"q": cleaned}),
            (None, {"search": cleaned}),
        ]
        last_error: Optional[Exception] = None
        for path, params in candidates:
            try:
                return self.fetch_feed(path, params=params)
            except CalibreOPDSError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        return self.fetch_feed()

    def download(self, href: str) -> DownloadedResource:
        if not href:
            raise ValueError("Download link missing")
        target = self._make_url(href)
        try:
            with self._open_client() as client:
                response = client.get(target, follow_redirects=True)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - thin wrapper
            raise CalibreOPDSError(
                f"Download failed with status {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - thin wrapper
            raise CalibreOPDSError(f"Download failed: {exc}") from exc

        mime_type = response.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        filename = self._deduce_filename(response, target, mime_type)
        return DownloadedResource(filename=filename, mime_type=mime_type, content=response.content)

    def _deduce_filename(self, response: httpx.Response, url: str, mime_type: str) -> str:
        header = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)"?', header)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate
        parsed = urlparse(url)
        stem = (parsed.path or "").strip("/").split("/")[-1]
        if not stem:
            stem = "download"
        if "." not in stem:
            extension = self._extension_for_mime(mime_type)
            if extension:
                stem = f"{stem}{extension}"
        return stem

    @staticmethod
    def _extension_for_mime(mime_type: str) -> str:
        normalized = mime_type.lower()
        if normalized in _EPUB_MIME_TYPES:
            return ".epub"
        if normalized == "application/pdf":
            return ".pdf"
        if normalized in {"text/plain", "text/html"}:
            return ".txt"
        return ""

    def _parse_feed(self, xml_payload: str, *, base_url: str) -> OPDSFeed:
        try:
            root = ET.fromstring(xml_payload)
        except ET.ParseError as exc:
            raise CalibreOPDSError(f"Unable to parse OPDS feed: {exc}") from exc

        feed_id = root.findtext("atom:id", default=None, namespaces=NS)
        feed_title = root.findtext("atom:title", default=None, namespaces=NS)
        links = self._parse_links(root.findall("atom:link", NS), base_url)
        entries = [self._parse_entry(node, base_url) for node in root.findall("atom:entry", NS)]
        return OPDSFeed(id=feed_id, title=feed_title, entries=entries, links=links)

    def _parse_entry(self, node: ET.Element, base_url: str) -> OPDSEntry:
        entry_id = node.findtext("atom:id", default="", namespaces=NS).strip()
        title = node.findtext("atom:title", default="Untitled", namespaces=NS).strip() or "Untitled"
        position_value = self._extract_position(node)
        updated = node.findtext("atom:updated", default=None, namespaces=NS)
        published = (
            node.findtext("dc:date", default=None, namespaces=NS)
            or node.findtext("atom:published", default=None, namespaces=NS)
        )
        if published:
            published = published.strip() or None

        summary_text = (
            self._extract_text(node.find("atom:summary", NS))
            or self._extract_text(node.find("atom:content", NS))
            or self._extract_text(node.find("dc:description", NS))
        )
        summary_metadata: Dict[str, str] = {}
        summary_body: Optional[str] = None
        if summary_text:
            summary_metadata, summary_body = self._split_summary_metadata(summary_text)
        cleaned_summary = self._strip_html(summary_body or summary_text)

        authors: List[str] = []
        for author_node in node.findall("atom:author", NS):
            name = author_node.findtext("atom:name", default="", namespaces=NS).strip()
            if name:
                authors.append(name)
        if not authors:
            creators = node.findall("dc:creator", NS)
            for creator in creators:
                value = (creator.text or "").strip()
                if value:
                    authors.append(value)

        links = node.findall("atom:link", NS)
        parsed_links = self._parse_links(links, base_url)
        download_link = self._select_download_link(parsed_links.values())
        alternate_link = parsed_links.get("alternate")
        thumb_link = parsed_links.get("http://opds-spec.org/image/thumbnail") or parsed_links.get(
            "thumbnail"
        )

        series_name = (
            node.findtext("calibre:series", default=None, namespaces=NS)
            or node.findtext("calibre_md:series", default=None, namespaces=NS)
        )
        if series_name:
            series_name = series_name.strip() or None

        series_index_raw = (
            node.findtext("calibre:series_index", default=None, namespaces=NS)
            or node.findtext("calibre_md:series_index", default=None, namespaces=NS)
        )
        series_index: Optional[float] = None
        if series_index_raw is not None:
            text = str(series_index_raw).strip()
            if text:
                try:
                    series_index = float(text)
                except ValueError:
                    match = re.search(r"\d+(?:\.\d+)?", text.replace(",", "."))
                    if match:
                        try:
                            series_index = float(match.group(0))
                        except ValueError:
                            series_index = None

        if series_name is None or series_index is None:
            category_series_name, category_series_index = self._extract_series_from_categories(
                node.findall("atom:category", NS)
            )
            if series_name is None and category_series_name:
                series_name = category_series_name
            if series_index is None and category_series_index is not None:
                series_index = category_series_index

        if (series_name is None or series_index is None) and summary_text:
            text_series_name, text_series_index = self._extract_series_from_text(summary_text)
            if series_name is None and text_series_name:
                series_name = text_series_name
            if series_index is None and text_series_index is not None:
                series_index = text_series_index

        tags_value = summary_metadata.get("TAGS")
        tags = self._parse_tags(tags_value) if tags_value else []
        rating_value = summary_metadata.get("RATING")
        rating, rating_max = self._parse_rating(rating_value) if rating_value else (None, None)

        return OPDSEntry(
            id=entry_id or title,
            title=title,
            position=position_value,
            authors=authors,
            updated=updated,
            published=published,
            summary=cleaned_summary,
            download=download_link,
            alternate=alternate_link,
            thumbnail=thumb_link,
            links=list(parsed_links.values()),
            series=series_name,
            series_index=series_index,
            tags=tags,
            rating=rating,
            rating_max=rating_max,
        )

    def _extract_series_from_categories(self, category_nodes: List[ET.Element]) -> tuple[Optional[str], Optional[float]]:
        name: Optional[str] = None
        index: Optional[float] = None
        for category in category_nodes:
            scheme = (category.attrib.get("scheme") or "").strip().lower()
            label = (category.attrib.get("label") or "").strip()
            term = (category.attrib.get("term") or "").strip()
            values: List[str] = []
            if label:
                values.append(label)
            if term and term not in values:
                values.append(term)

            is_series_hint = "series" in scheme or any("series" in value.lower() for value in values if value)
            if not is_series_hint:
                continue

            for value in values:
                if not value:
                    continue
                candidate_name, candidate_index = self._parse_series_value(value)
                if candidate_name and not name:
                    name = candidate_name
                if candidate_index is not None and index is None:
                    index = candidate_index
                if name and index is not None:
                    return name, index
        return name, index

    def _parse_series_value(self, value: str) -> tuple[Optional[str], Optional[float]]:
        cleaned = re.sub(r"\s+", " ", value or "").strip()
        if not cleaned:
            return None, None
        cleaned = _SERIES_PREFIX_RE.sub("", cleaned)
        working = cleaned
        number: Optional[float] = None

        bracket_match = _SERIES_NUMBER_BRACKET_RE.search(working)
        if bracket_match:
            number = self._coerce_series_index(bracket_match.group(1))
            start, end = bracket_match.span()
            working = (working[:start] + working[end:]).strip()

        if number is None:
            hash_match = _SERIES_NUMBER_HASH_RE.search(working)
            if hash_match:
                number = self._coerce_series_index(hash_match.group(1))
                start, end = hash_match.span()
                working = (working[:start] + working[end:]).strip()

        if number is None:
            book_match = _SERIES_NUMBER_BOOK_RE.search(working)
            if book_match:
                number = self._coerce_series_index(book_match.group(1))
                start, end = book_match.span()
                working = (working[:start] + working[end:]).strip()

        name = working.strip(" -–—,:")
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            name = None
        return name, number

    @staticmethod
    def _extract_text(node: Optional[ET.Element]) -> Optional[str]:
        if node is None:
            return None
        # Prefer itertext to capture nested XHTML content
        parts = list(node.itertext())
        if not parts:
            return (node.text or "").strip() or None
        combined = "".join(parts).strip()
        return combined or None

    def _extract_series_from_text(self, text: str) -> tuple[Optional[str], Optional[float]]:
        for line in text.splitlines():
            match = _SERIES_LINE_TEXT_RE.match(line)
            if not match:
                continue
            candidate = match.group(1).strip()
            if not candidate:
                continue
            name, number = self._parse_series_value(candidate)
            if name or number is not None:
                return name, number
        return None, None

    def _split_summary_metadata(self, text: Optional[str]) -> tuple[Dict[str, str], Optional[str]]:
        metadata: Dict[str, str] = {}
        if text is None:
            return metadata, None
        lines = text.splitlines()
        index = 0
        total = len(lines)
        while index < total and not lines[index].strip():
            index += 1
        while index < total:
            stripped = lines[index].strip()
            if not stripped:
                break
            match = _SUMMARY_METADATA_LINE_RE.match(stripped)
            if not match:
                break
            key = match.group(1).strip().upper()
            value = match.group(2).strip()
            if key and value:
                metadata[key] = value
            index += 1
        remainder = "\n".join(lines[index:]).strip()
        return metadata, (remainder or None)

    @staticmethod
    def _parse_tags(value: str) -> List[str]:
        if not value:
            return []
        tokens = re.split(r"[;,\n]\s*", value)
        cleaned: List[str] = []
        seen: set[str] = set()
        for token in tokens:
            entry = token.strip()
            if not entry:
                continue
            key = entry.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(entry)
        return cleaned

    @staticmethod
    def _parse_rating(value: str) -> tuple[Optional[float], Optional[float]]:
        if not value:
            return None, None
        text = value.strip()
        if not text:
            return None, None
        stars = text.count("★")
        half = 0.5 if "½" in text else 0.0
        if stars or half:
            rating = stars + half
            return (rating if rating > 0 else None, 5.0)
        match = re.search(r"\d+(?:\.\d+)?", text.replace(",", "."))
        if match:
            try:
                rating_value = float(match.group(0))
            except ValueError:
                return None, None
            return rating_value, 5.0
        return None, None

    @staticmethod
    def _coerce_series_index(value: str) -> Optional[float]:
        text = value.strip().replace(",", ".")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _extract_position(self, node: ET.Element) -> Optional[int]:
        candidates = [
            node.findtext("opds:position", default=None, namespaces=NS),
            node.findtext("opds:groupPosition", default=None, namespaces=NS),
            node.findtext("opds:order", default=None, namespaces=NS),
            node.findtext("dc:identifier", default=None, namespaces=NS),
        ]
        for value in candidates:
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            try:
                return int(float(text))
            except (TypeError, ValueError):
                continue
        return None

    def _parse_links(self, link_nodes: List[ET.Element], base_url: str) -> Dict[str, OPDSLink]:
        results: Dict[str, OPDSLink] = {}
        for link in link_nodes:
            href = link.attrib.get("href")
            if not href:
                continue
            rel = link.attrib.get("rel")
            link_type = link.attrib.get("type")
            title = link.attrib.get("title")
            base_for_join = base_url or self._base_url
            absolute_href = urljoin(base_for_join, href)
            entry = OPDSLink(href=absolute_href, rel=rel, type=link_type, title=title)
            key = rel or absolute_href
            results[key] = entry
        return results

    @staticmethod
    def _select_download_link(links: Mapping[str, OPDSLink] | Iterable[OPDSLink]) -> Optional[OPDSLink]:
        if isinstance(links, Mapping):
            iterable: List[OPDSLink] = list(links.values())
        else:
            iterable = list(links)
        best: Optional[OPDSLink] = None
        for link in iterable:
            rel = (link.rel or "").lower()
            if "acquisition" not in rel:
                continue
            mime = (link.type or "").lower()
            if mime in _EPUB_MIME_TYPES:
                return link
            if best is None:
                best = link
        if best:
            return best
        # Fallback to first epub-like link even without explicit acquisition rel
        for link in iterable:
            mime = (link.type or "").lower()
            if mime in _EPUB_MIME_TYPES:
                return link
        # No valid acquisition-style link exposed
        return None


def feed_to_dict(feed: OPDSFeed) -> Dict[str, Any]:
    """Helper used by APIs to convert a feed into JSON-serialisable payloads."""

    return feed.to_dict()
