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
    try:
        from pypdf import PdfReader
    except ImportError:
        return content.decode("utf-8", errors="ignore")[:MAX_TEXT_CHARS]
    reader = PdfReader(io.BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)[:MAX_TEXT_CHARS]


def extract_pdf_links(content: bytes, source_url: str | None = None) -> list[DocumentLink]:
    links: list[DocumentLink] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        for page in reader.pages:
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
    except (ImportError, TypeError, ValueError, KeyError):
        pass
    # Some providers expose URLs as plain text rather than PDF annotations.
    text = extract_pdf_text(content)
    for raw in _URL_RE.findall(text):
        url = _clean_url(raw)
        links.append(
            DocumentLink(
                url, url, source_url, kind="minutes" if _MINUTES_RE.search(url) else "backup"
            )
        )
    return _dedupe_links(links)


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


def extract_document(
    content: bytes, *, content_type: str = "", source_url: str | None = None
) -> tuple[str, list[DocumentLink]]:
    is_pdf = "pdf" in content_type.lower() or (source_url or "").lower().split("?", 1)[0].endswith(
        ".pdf"
    )
    if is_pdf:
        return extract_pdf_text(content), extract_pdf_links(content, source_url)
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
    votes: list[dict] = []
    for line in text.splitlines():
        if not re.search(r"\b(vote|ayes?|nays?|motion|approved|opposed)\b", line, re.I):
            continue
        for name, value in re.findall(
            r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,3})\s*[-:–]\s*(yes|no|absent|recused|abstain(?:ed)?)\b",
            line,
            re.I,
        ):
            votes.append(
                {
                    "agenda_item": None,
                    "member": name.strip(),
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
                    votes.append(
                        {
                            "agenda_item": None,
                            "member": name,
                            "value": value.lower().replace("abstained", "abstain"),
                            "evidence": line[:500],
                        }
                    )
    return votes
