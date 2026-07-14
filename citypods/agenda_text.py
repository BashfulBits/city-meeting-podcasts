"""Bounded extraction of agenda/packet/minutes documents.

The provider link is always the authority.  This module only extracts text and document links;
the stages decide which link may be persisted and retain source URLs/evidence for auditability.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from urllib.parse import urljoin

_MINUTES_RE = re.compile(r"\bminutes?\b", re.I)
_AGENDA_RE = re.compile(r"\bagenda|packet|backup|attachment|supporting\b", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
MAX_TEXT_CHARS = 1_000_000
MAX_LINKS = 200
AGENDA_TEXT_MAX_CHARS = 50_000
BACKUP_ITEM_MAX_CHARS = 20_000


@dataclass(frozen=True)
class DocumentLink:
    url: str
    label: str = ""
    source_url: str | None = None
    item_label: str | None = None
    kind: str = "backup"


def _clean_url(url: str) -> str:
    return url.rstrip('.,);]}>"')


def extract_pdf_text(content: bytes) -> str:
    return _extract_pdf(content)[0]


def extract_pdf_links(content: bytes, source_url: str | None = None) -> list[DocumentLink]:
    return _extract_pdf(content, source_url=source_url)[1]


def _extract_pdf(
    content: bytes, *, source_url: str | None = None
) -> tuple[str, list[DocumentLink]]:
    """Extract both text and URI annotations from one PdfReader pass."""
    try:
        from pypdf import PdfReader
    except ImportError:
        text = content.decode("utf-8", errors="ignore")[:MAX_TEXT_CHARS]
        return text, _links_from_text(text, source_url)
    links: list[DocumentLink] = []
    texts: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(content))
        for page in reader.pages:
            texts.append(page.extract_text() or "")
            for annot in page.get("/Annots", []) or []:
                obj = annot.get_object()
                action = obj.get("/A") if obj else None
                uri = action.get("/URI") if action else None
                if uri:
                    url = _clean_url(str(uri))
                    links.append(
                        DocumentLink(
                            url,
                            url,
                            source_url,
                            kind="minutes" if _MINUTES_RE.search(url) else "backup",
                        )
                    )
    except (TypeError, ValueError, KeyError):
        pass
    text = "\n".join(texts)[:MAX_TEXT_CHARS]
    return text, _dedupe_links(links + _links_from_text(text, source_url))


def _links_from_text(text: str, source_url: str | None) -> list[DocumentLink]:
    return [
        DocumentLink(
            _clean_url(raw),
            _clean_url(raw),
            source_url,
            kind="minutes" if _MINUTES_RE.search(raw) else "backup",
        )
        for raw in _URL_RE.findall(text)
    ]


def extract_html(
    content: bytes | str, source_url: str | None = None
) -> tuple[str, list[DocumentLink]]:
    raw = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else content
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _URL_RE.sub("", raw)[:MAX_TEXT_CHARS], _dedupe_links(
            [
                DocumentLink(
                    _clean_url(u),
                    u,
                    source_url,
                    kind="minutes" if _MINUTES_RE.search(u) else "backup",
                )
                for u in _URL_RE.findall(raw)
            ]
        )
    soup = BeautifulSoup(raw, "html.parser")
    for node in soup.find_all(["script", "style", "nav", "footer", "noscript"]):
        node.decompose()
    links: list[DocumentLink] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(source_url or "", str(anchor.get("href")))
        if not href.lower().startswith("http"):
            continue
        label = " ".join(anchor.get_text(" ", strip=True).split())
        haystack = f"{label} {href}"
        kind = "minutes" if _MINUTES_RE.search(haystack) else "backup"
        if kind == "backup" and not (_AGENDA_RE.search(haystack) or ".pdf" in href.lower()):
            continue
        links.append(DocumentLink(_clean_url(href), label, source_url, label or None, kind))
    return soup.get_text("\n", strip=True)[:MAX_TEXT_CHARS], _dedupe_links(links)


def extract_agenda_pdf(content: bytes) -> str:
    return extract_pdf_text(content)


def extract_agenda_html(content: bytes) -> str:
    return extract_html(content)[0]


def extract_agenda_text(
    agenda_url: str | None, agenda_portal_url: str | None, session
) -> str | None:
    url = agenda_portal_url or agenda_url
    if not url:
        return None
    try:
        from citypods.http import fetch_document_bytes

        content, content_type = fetch_document_bytes(session, url, timeout=30)
        text, _ = extract_document(
            content,
            content_type=content_type,
            source_url=url,
        )
        text = re.sub(r"\s+", " ", text).strip()[:AGENDA_TEXT_MAX_CHARS]
        return text if len(text) >= 20 else None
    except Exception:
        return None


def attribute_links_to_chapters(
    links: list[tuple[int, str]], chapters: list[dict], pdf_page_count: int
) -> list[tuple[int | None, str | None, str]]:
    if not chapters:
        return [(None, None, url) for _, url in links]
    total_pages = max(1, pdf_page_count)
    out: list[tuple[int | None, str | None, str]] = []
    for page_index, url in links:
        chapter_index = min(len(chapters) - 1, (max(0, page_index) * len(chapters)) // total_pages)
        out.append((chapter_index, chapters[chapter_index].get("title"), url))
    return out


def extract_backup_item(url: str, session) -> tuple[str | None, bool]:
    try:
        from citypods.http import fetch_document_bytes

        content, content_type = fetch_document_bytes(session, url, timeout=30)
        text, _ = extract_document(
            content,
            content_type=content_type,
            source_url=url,
        )
        truncated = len(text) > BACKUP_ITEM_MAX_CHARS
        return (text[:BACKUP_ITEM_MAX_CHARS].strip() or None), truncated
    except Exception:
        return None, False


def extract_document(
    content: bytes, *, content_type: str = "", source_url: str | None = None
) -> tuple[str, list[DocumentLink]]:
    is_pdf = "pdf" in content_type.lower() or (source_url or "").lower().split("?", 1)[0].endswith(
        ".pdf"
    )
    if is_pdf:
        return _extract_pdf(content, source_url=source_url)
    return extract_html(content, source_url)


def _dedupe_links(links: list[DocumentLink]) -> list[DocumentLink]:
    out: list[DocumentLink] = []
    seen: set[str] = set()
    for link in links:
        if link.url in seen:
            continue
        seen.add(link.url)
        out.append(link)
        if len(out) >= MAX_LINKS:
            break
    return out


def minutes_links(links: list[DocumentLink]) -> list[DocumentLink]:
    return [
        link
        for link in links
        if link.kind == "minutes" or _MINUTES_RE.search(f"{link.label} {link.url}")
    ]


def parse_roster(text: str) -> list[dict]:
    """Conservative member extraction from attendance lines; never invents names."""
    result: list[dict] = []
    for match in re.finditer(r"(?im)^\s*(present|absent|excused|recused)\s*:\s*(.+)$", text):
        status = match.group(1).lower()
        for name in re.split(r",|;|\s{2,}|\s+and\s+", match.group(2).strip()):
            name = re.sub(r"\s+", " ", name).strip(" .")
            if name and len(name) > 1:
                result.append({"name": name, "status": status, "evidence": match.group(0)[:500]})
    return result


def parse_votes(text: str, *, roster: list[dict] | None = None) -> list[dict]:
    """Extract explicit per-member vote labels from simple minutes notation.

    Ambiguous prose is intentionally ignored.  Each returned item includes the original evidence.
    """
    allowed = {"yes", "no", "absent", "recused", "abstain", "abstained"}
    roster_names = {
        _name_key(str(member.get("name"))): str(member["name"])
        for member in (roster or [])
        if isinstance(member, dict) and member.get("name")
    }

    def canonical_member(name: str) -> str | None:
        if roster is None:
            return name
        return roster_names.get(_name_key(name))

    votes: list[dict] = []
    current_item: str | None = None
    for line in text.splitlines():
        heading = re.match(r"\s*(?:item\s+)?(\d+[A-Za-z.]*)[.)\-:]\s*(.+)$", line, re.I)
        if heading:
            current_item = heading.group(2).strip()
        if not re.search(r"\b(vote|ayes?|nays?|motion|approved|opposed)\b", line, re.I):
            continue
        for name, value in re.findall(
            r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,3})\s*[-:–]\s*(yes|no|absent|recused|abstain(?:ed)?)\b",
            line,
            re.I,
        ):
            member = canonical_member(name.strip())
            if member is None:
                continue
            votes.append(
                {
                    "agenda_item": current_item,
                    "member": member,
                    "value": value.lower().replace("abstained", "abstain"),
                    "evidence": line[:500],
                }
            )
        for value, names in re.findall(
            r"\b(yes|no|absent|recused|abstain(?:ed)?)\s*[:=-]\s*([^;]+)", line, re.I
        ):
            for name in re.split(r",|\band\b", names):
                name = name.strip(" .")
                if name and name.lower() not in allowed:
                    member = canonical_member(name)
                    if member is None:
                        continue
                    votes.append(
                        {
                            "agenda_item": current_item,
                            "member": member,
                            "value": value.lower().replace("abstained", "abstain"),
                            "evidence": line[:500],
                        }
                    )
    return votes


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())
