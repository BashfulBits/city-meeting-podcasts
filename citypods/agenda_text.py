"""Bounded extraction of agenda/packet/minutes documents.

The provider link is always the authority.  This module only extracts text and document links;
the stages decide which link may be persisted and retain source URLs/evidence for auditability.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urljoin

_MINUTES_RE = re.compile(r"\bminutes?\b", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
# A short alphanumeric case/item identifier such as "PD20-25", "ZA20-8", "SUP20-6", or "2026-0071"
# -- confirmed, on real live Granicus and Legistar agendas, to be the identifier a backup
# document's own filename/label or a per-item detail page repeats verbatim next to its parent
# item's title. Matching on this (or the full title text) is what replaces English-keyword
# matching (agenda/packet/backup/attachment/supporting) as the backup-document classification
# signal -- keyword phrases are English- and convention-specific; an item identifier embedded in
# a document's own name is not.
_ITEM_ID_RE = re.compile(r"\b[A-Za-z]{1,4}-?\d{2,4}(?:-\d{1,4})?\b")
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


@dataclass(frozen=True)
class AgendaTitleCandidate:
    """A structural main-agenda title candidate, with its source line for auditability."""

    title: str
    line_number: int


_BARE_NUMBER_RE = re.compile(r"^(?:\d+|[IVXLC]+)\.$", re.I)
_BARE_LETTER_RE = re.compile(r"^[A-Z]\.$")
_NUMBERED_TITLE_RE = re.compile(
    r"^(?P<prefix>(?:\d+|[IVXLC]+)(?:\.\s*[A-Z])?\.)\s*(?P<title>\S.*)$", re.I
)
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+(?P<title>\S.*?)(?:\s+#+)?$")
_ALL_CAPS_TITLE_RE = re.compile(r"^[A-Z][A-Z0-9 &'’/,:;()\-]{2,}$")
_AGENDA_MARKER_RE = re.compile(r"^AGENDA(?:\s+ITEMS?)?$", re.I)
_NON_TITLE_LABELS = frozenset(
    {
        "AGENDA",
        "AGENDA ITEM",
        "AGENDA ITEMS",
        "ATTACHMENT",
        "EXECUTIVE SESSION",
        "NOTICE OF PUBLIC MEETING",
        "CURRENT COMMISSIONERS",
        "EX-OFFICIO MEMBERS",
    }
)


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


def extract_pdf_layout_text(content: bytes) -> str:
    """Extract a PDF's visual reading layout using the existing pypdf dependency.

    This is intentionally a lightweight representation rather than a lossy PDF-to-HTML or OCR
    conversion. It preserves indentation/line separation for agenda heading experiments while the
    normal text extractor remains the stable, bounded artifact used elsewhere.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return content.decode("utf-8", errors="ignore")[:MAX_TEXT_CHARS]
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(
            page.extract_text(extraction_mode="layout") or "" for page in reader.pages
        )[:MAX_TEXT_CHARS]
    except (TypeError, ValueError, KeyError):
        return _extract_pdf(content)[0]


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
        # Every same-origin-validated link on the page becomes a backup-document candidate --
        # not gated on English keywords (agenda/packet/backup/attachment/supporting), since a
        # different city's agenda platform may label these links entirely differently (or not
        # label them with words at all, e.g. Legistar's bare "File #" links). Bounded by
        # MAX_LINKS and, at the fetch site (AgendaTextStage), by the same SSRF-gated
        # same-origin/domain checks every other candidate link already goes through -- this is a
        # classification change, not a change to what is safe to fetch. Real attribution to a
        # specific agenda item happens later (attribute_links_by_content / the position-based
        # fallback), not here.
        kind = "minutes" if _MINUTES_RE.search(haystack) else "backup"
        links.append(DocumentLink(_clean_url(href), label, source_url, label or None, kind))
    return soup.get_text("\n", strip=True)[:MAX_TEXT_CHARS], _dedupe_links(links)


def extract_html_outline(content: bytes | str) -> str:
    """Return a small Markdown-like outline from semantic HTML agenda headings.

    Standard heading elements are retained, as are Granicus-style links carrying an ``Agenda``
    CSS class. This supplements, rather than replaces, the full plain text: it deliberately does
    not infer headings from typography such as arbitrary bold text.
    """
    raw = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else content
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    lines: list[str] = []
    seen: set[str] = set()
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "a"]):
        if node.name == "a":
            classes = node.get("class") or []
            if not any(str(css_class).casefold() == "agenda" for css_class in classes):
                continue
            level = 2
        else:
            level = int(node.name[1])
        title = _normalize_ws(node.get_text(" ", strip=True))
        key = title.casefold()
        if len(title) < 3 or key in seen:
            continue
        seen.add(key)
        lines.append(f"{'#' * level} {title}")
    return "\n".join(lines)[:MAX_TEXT_CHARS]


def extract_agenda_outline(
    content: bytes, *, content_type: str = "", source_url: str | None = None
) -> str:
    """Create a lightweight format-aware agenda outline for title-selection evaluation.

    Callers preserve it separately from ``extract_document``'s full text so a future prompt can
    use visual/semantic heading evidence without losing the original searchable agenda artifact.
    """
    is_pdf = "pdf" in content_type.lower() or (source_url or "").lower().split("?", 1)[0].endswith(
        ".pdf"
    )
    return extract_pdf_layout_text(content) if is_pdf else extract_html_outline(content)


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


def extract_agenda_title_candidates(
    text: str, *, max_items: int = 200
) -> list[AgendaTitleCandidate]:
    """Return conservative, line-structured title candidates from a main agenda.

    This is intentionally a *candidate* extractor, not a claim that every listed line was
    discussed or deserves a chapter. It recognizes numbered items and all-caps section headings
    after an ``AGENDA`` marker when present, while retaining source-line evidence for benchmark
    review. Backup/packet text is never passed here.
    """
    if max_items < 1:
        raise ValueError("max_items must be positive")
    candidates: list[AgendaTitleCandidate] = []
    seen: set[str] = set()
    after_marker = False
    pending_prefix: str | None = None
    current_section_prefix: str | None = None

    def add(title: str, line_number: int) -> None:
        normalized = _normalize_ws(title)
        key = normalized.casefold()
        if (
            len(normalized) < 3
            or len(normalized) > 2_000
            or key in {label.casefold() for label in _NON_TITLE_LABELS}
            or key in seen
            or len(candidates) >= max_items
        ):
            return
        seen.add(key)
        candidates.append(AgendaTitleCandidate(normalized, line_number))

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line_number, raw_line in enumerate(lines, start=1):
        line = _normalize_ws(raw_line)
        if not line:
            continue
        if _AGENDA_MARKER_RE.fullmatch(line):
            after_marker = True
            pending_prefix = None
            current_section_prefix = None
            continue
        markdown_heading = _MARKDOWN_HEADING_RE.fullmatch(line)
        if markdown_heading:
            add(markdown_heading.group("title"), line_number)
            continue
        if _BARE_LETTER_RE.fullmatch(line) and (pending_prefix or current_section_prefix):
            pending_prefix = f"{pending_prefix or current_section_prefix} {line}"
            continue
        if _BARE_NUMBER_RE.fullmatch(line):
            pending_prefix = line
            current_section_prefix = line
            continue
        numbered = _NUMBERED_TITLE_RE.fullmatch(line)
        if numbered:
            prefix = numbered.group("prefix")
            section = re.match(r"^(?:\d+|[IVXLC]+)\.", prefix, re.I)
            if section:
                current_section_prefix = section.group(0)
            add(f"{prefix} {numbered.group('title')}", line_number)
            pending_prefix = None
            continue
        if pending_prefix:
            # PDF extraction often puts an item's numeric prefix and its title on separate lines.
            # Consume exactly the immediate meaningful continuation; attachment labels should not
            # become titles and leave the prefix available for the actual following title.
            if line.casefold() not in {"attachment:", "attachment"}:
                add(f"{pending_prefix} {line}", line_number)
                pending_prefix = None
            continue
        if after_marker and _ALL_CAPS_TITLE_RE.fullmatch(line):
            add(line, line_number)
    return candidates


def agenda_title_similarity(left: str, right: str) -> float:
    """Return tolerant title similarity for extraction evaluation, not an attribution claim."""

    def normalize(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", _normalize_ws(value).casefold()).strip()

    left_norm, right_norm = normalize(left), normalize(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm in right_norm or right_norm in left_norm:
        return 1.0
    return SequenceMatcher(a=left_norm, b=right_norm).ratio()


def attribute_links_to_chapters(
    links: list[tuple[int, str]], chapters: list[dict], pdf_page_count: int
) -> list[tuple[int | None, str | None, str]]:
    """Order-based fallback attribution: distribute links across chapters proportionally by
    page-order position. Kept as the graceful fallback (review/29 §6a's original design) for when
    ``attribute_links_by_content`` finds no identifier/title match at all -- not guaranteed
    precise, good enough for "which item is this probably about" rather than a hard guarantee."""
    if not chapters:
        return [(None, None, url) for _, url in links]
    total_pages = max(1, pdf_page_count)
    out: list[tuple[int | None, str | None, str]] = []
    for page_index, url in links:
        chapter_index = min(len(chapters) - 1, (max(0, page_index) * len(chapters)) // total_pages)
        out.append((chapter_index, chapters[chapter_index].get("title"), url))
    return out


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def item_identifiers(title: str) -> list[str]:
    """Short alphanumeric case/item identifiers found in a chapter title (e.g. "PD20-25" out of
    "Zoning Case PD20-25"). Confirmed, on real Granicus and Legistar agendas, to be the string a
    backup document's own filename/label or per-item detail page repeats verbatim -- a stronger,
    more broadly generalizable signal than the title's full text (which may be paraphrased or
    truncated in a document label) or page/text position alone."""
    return [m.group(0) for m in _ITEM_ID_RE.finditer(title)]


def chapter_text_matches(candidate_text: str, chapter_title: str) -> bool:
    """Does ``candidate_text`` (a link's own label/URL, or a fetched page's extracted text)
    plausibly belong to ``chapter_title``? Prefers an item identifier match (see
    ``item_identifiers``); falls back to the whitespace-normalized full title as a substring."""
    if not candidate_text or not chapter_title:
        return False
    haystack = _normalize_ws(candidate_text).casefold()
    identifiers = item_identifiers(chapter_title)
    if identifiers:
        # A boundary-aware match, not raw containment: "PD20-2" is a substring of
        # "PD20-25" (a different, longer case number), so a plain `in` check would attribute a
        # backup document to the wrong item whenever one case number is a prefix of another.
        return any(
            re.search(rf"(?<![0-9a-z]){re.escape(identifier.casefold())}(?![0-9a-z])", haystack)
            for identifier in identifiers
        )
    return _normalize_ws(chapter_title).casefold() in haystack


def attribute_links_by_content(
    links: list[DocumentLink], chapters: list[dict]
) -> list[tuple[int | None, str | None, DocumentLink]]:
    """Attribute each link to a chapter by matching an item/case identifier or the chapter title
    itself against the link's own label/URL text -- platform-agnostic (validated against real
    Legistar and Granicus agendas, see review/29), unlike ``attribute_links_to_chapters``'s
    page-position proxy. A link that matches no chapter is left unattributed (``None``) rather
    than guessed; callers should fall back to ``attribute_links_to_chapters`` for those."""
    result: list[tuple[int | None, str | None, DocumentLink]] = []
    for link in links:
        haystack = f"{link.label or ''} {link.url}"
        matched: tuple[int, str] | None = None
        for index, chapter in enumerate(chapters):
            title = str(chapter.get("title") or "")
            if title and chapter_text_matches(haystack, title):
                matched = (index, title)
                break
        if matched is None:
            result.append((None, None, link))
        else:
            result.append((matched[0], matched[1], link))
    return result


def resolve_chapter_spans(
    text: str, chapter_titles: list[str]
) -> tuple[str, list[tuple[int, int] | None]]:
    """Locate each chapter title, in order, within ``text`` and return (a) the
    whitespace-normalized/casefolded text those offsets are valid against, and (b) one
    ``(start, end)`` span per chapter -- text between title *i* and title *i+1* belongs to
    chapter *i*. A title not found (out of order, paraphrased, or simply absent from this
    document) resolves to ``None`` for that chapter rather than a guessed span; the next
    found title still anchors the span *before* it correctly, since the search cursor only
    advances on an actual match.

    Text before the first resolved title (meeting-notice boilerplate, hearing sign-up
    instructions -- typically NOT part of any agenda item) belongs to no chapter and is exactly
    what callers should treat as excluded preamble.
    """
    norm_text = _normalize_ws(text).casefold()
    starts: list[int | None] = []
    cursor = 0
    for title in chapter_titles:
        norm_title = _normalize_ws(title).casefold()
        if not norm_title:
            starts.append(None)
            continue
        idx = norm_text.find(norm_title, cursor)
        if idx == -1:
            starts.append(None)
            continue
        starts.append(idx)
        cursor = idx + len(norm_title)
    spans: list[tuple[int, int] | None] = []
    for i, start in enumerate(starts):
        if start is None:
            spans.append(None)
            continue
        end = next((nxt for nxt in starts[i + 1 :] if nxt is not None), len(norm_text))
        spans.append((start, end))
    return norm_text, spans


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
