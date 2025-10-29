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
NS = {"atom": ATOM_NS, "opds": OPDS_NS, "dc": DC_NS}


_TAG_STRIP_RE = re.compile(r"<[^>]+>")
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
    authors: List[str] = field(default_factory=list)
    updated: Optional[str] = None
    summary: Optional[str] = None
    download: Optional[OPDSLink] = None
    alternate: Optional[OPDSLink] = None
    thumbnail: Optional[OPDSLink] = None
    links: List[OPDSLink] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "authors": list(self.authors),
            "updated": self.updated,
            "summary": self.summary,
            "download": self.download.to_dict() if self.download else None,
            "alternate": self.alternate.to_dict() if self.alternate else None,
            "thumbnail": self.thumbnail.to_dict() if self.thumbnail else None,
            "links": [link.to_dict() for link in self.links],
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
        return urljoin(self._base_url, href)

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
        updated = node.findtext("atom:updated", default=None, namespaces=NS)
        summary = node.findtext("atom:summary", default=None, namespaces=NS) or node.findtext(
            "atom:content", default=None, namespaces=NS
        )
        dc_summary = node.findtext("dc:description", default=None, namespaces=NS)
        summary = summary or dc_summary
        cleaned_summary = self._strip_html(summary)
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
        return OPDSEntry(
            id=entry_id or title,
            title=title,
            authors=authors,
            updated=updated,
            summary=cleaned_summary,
            download=download_link,
            alternate=alternate_link,
            thumbnail=thumb_link,
            links=list(parsed_links.values()),
        )

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
