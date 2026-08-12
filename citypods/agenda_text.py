"""Bounded extraction of agenda/packet/minutes documents.

The provider link is always the authority.  This module only extracts text and document links;
the stages decide which link may be persisted and retain source URLs/evidence for auditability.
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
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
# Bumped 2 -> 3 (GH#1092): line-preserving placeholder classification, bounded native/OCR
# similarity, and scaled OCR budgets change the persisted quality decision.
AGENDA_TEXT_QUALITY_VERSION = "3"
OCR_MAX_PAGES = 120
OCR_MAX_OUTPUT_CHARS = AGENDA_TEXT_MAX_CHARS
OCR_PROBE_TIMEOUT_SECONDS = 15
OCR_FULL_TIMEOUT_SECONDS = 60
OCR_FULL_SECONDS_PER_PAGE = 5
OCR_MIN_CONFIDENCE = 60.0
OCR_MIN_ALPHA_CHARS = 100
_SIMILARITY_SAMPLE_CHARS = 8_000
_SIMILARITY_ACCEPTANCE_THRESHOLD = 0.6


class OcrUnavailableError(RuntimeError):
    """Raised when the bounded OCR fallback cannot find its required executables."""


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


@dataclass(frozen=True)
class AgendaTextAssessment:
    """Bounded, explainable admission decision for one agenda candidate."""

    text: str
    source_url: str
    source_type: str
    method: str
    status: str
    eligibility: str
    reason: str
    pipeline_version: str = AGENDA_TEXT_QUALITY_VERSION
    native_chars: int = 0
    ocr_chars: int = 0
    page_count: int = 0
    native_ocr_similarity: float | None = None
    ocr_mean_confidence: float | None = None
    truncated: bool = False

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "eligibility": self.eligibility,
            "method": self.method,
            "reason": self.reason,
            "pipeline_version": self.pipeline_version,
            "source_url": self.source_url,
            "native_chars": self.native_chars,
            "ocr_chars": self.ocr_chars,
            "page_count": self.page_count,
            "native_ocr_similarity": self.native_ocr_similarity,
            "ocr_mean_confidence": self.ocr_mean_confidence,
            "truncated": self.truncated,
        }


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


_PLACEHOLDER_RE = re.compile(
    r"(?:documentviewer\.php|\bloading(?:\.\.\.|…)?\b|not currently published|"
    r"please wait|javascript (?:is )?required|an error occurred|unable to load)",
    re.I,
)
_NOTICE_RE = re.compile(
    r"\b(?:cancel(?:led|ed)?|no meeting|meeting (?:is )?(?:postponed|rescheduled)|"
    r"meeting notice|meeting has been canceled)\b",
    re.I,
)
_AGENDA_NUMBER_RE = re.compile(r"(?:^|\n)\s*(?:\d+|[IVXLC]+)(?:\.[A-Z])?\.", re.I)


def _alpha_chars(text: str) -> int:
    return sum(char.isalpha() for char in text)


def _repeated_line_ratio(text: str) -> float:
    lines = [_normalize_ws(line).casefold() for line in text.splitlines() if _normalize_ws(line)]
    if len(lines) < 3:
        return 0.0
    return 1.0 - (len(set(lines)) / len(lines))


def agenda_content_score(text: str) -> int:
    """Return a small deterministic score for visible agenda-like content."""
    if not text:
        return 0
    normalized = _normalize_ws(text).casefold()
    score = 0
    if re.search(r"\bagenda(?: items?)?\b", normalized):
        score += 1
    if _AGENDA_NUMBER_RE.search(text):
        score += 2
    if extract_agenda_title_candidates(text, max_items=20):
        score += 2
    if _alpha_chars(text) >= 500:
        score += 1
    return score


def _is_placeholder_text(text: str) -> bool:
    normalized = _normalize_ws(text)
    if not normalized:
        return True
    if _PLACEHOLDER_RE.search(normalized):
        # A real agenda can contain the word "loading" in a footnote, so only classify it as a
        # placeholder when the document is otherwise short/boilerplate or the shell signature is
        # unambiguous.
        if "documentviewer.php" in normalized.casefold() or "not currently published" in (
            normalized.casefold()
        ):
            return True
        if len(normalized) <= 500 or agenda_content_score(text) == 0:
            return True
    compact = re.sub(r"[^a-z0-9]+", "", normalized.casefold())
    return (
        bool(compact)
        and len(normalized) <= 180
        and (
            normalized.lower().startswith(("http://", "https://"))
            or bool(re.fullmatch(r"[\w./-]+(?:\.pdf)?", normalized, re.I))
        )
    )


def _is_short_notice(text: str) -> bool:
    return bool(text and len(text) <= 1_000 and _NOTICE_RE.search(text))


def classify_agenda_text(text: str) -> str:
    """Return the shared coarse class used by production and chapter research."""
    normalized = _normalize_ws(text)
    if not normalized:
        return "empty"
    if "not currently published" in normalized.casefold():
        return "unpublished-placeholder"
    if _is_placeholder_text(text):
        return "viewer-placeholder"
    if _is_short_notice(normalized):
        return "short-notice"
    return "complete"


def agenda_chapter_eligible(quality: dict | None) -> bool:
    """Whether an accepted artifact is suitable input for agenda-item/chapter extraction."""
    return bool(
        isinstance(quality, dict)
        and quality.get("status") == "accepted"
        and quality.get("eligibility") == "agenda"
    )


def _pdf_page_count(content: bytes) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(content)).pages)
    except Exception:  # noqa: BLE001 - page count is diagnostic, not an extraction prerequisite
        return 0


def _ocr_page_numbers(page_count: int) -> list[int]:
    if page_count <= 0:
        return [1]
    pages = {1, min(page_count, OCR_MAX_PAGES)}
    if page_count > 2:
        pages.add((page_count + 1) // 2)
    return sorted(pages)


def _ocr_pdf_pages(
    content: bytes,
    pages: list[int],
    *,
    timeout: int,
) -> tuple[str, float | None]:
    """Render selected PDF pages and return Tesseract text plus mean word confidence."""
    if not shutil.which("pdftocairo") or not shutil.which("tesseract"):
        raise OcrUnavailableError
    with tempfile.TemporaryDirectory(prefix="citypods-ocr-") as temp_dir:
        root = Path(temp_dir)
        pdf_path = root / "agenda.pdf"
        pdf_path.write_bytes(content)
        texts: list[str] = []
        confidences: list[float] = []
        deadline = time.monotonic() + timeout
        for page in pages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("ocr", timeout)
            image_prefix = root / f"page-{page}"
            subprocess.run(
                [
                    "pdftocairo",
                    "-png",
                    "-scale-to",
                    "2400",
                    "-singlefile",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    str(pdf_path),
                    str(image_prefix),
                ],
                check=True,
                capture_output=True,
                timeout=remaining,
            )
            image_path = image_prefix.with_suffix(".png")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("ocr", timeout)
            proc = subprocess.run(
                ["tesseract", str(image_path), "stdout", "--psm", "6", "tsv"],
                check=True,
                capture_output=True,
                text=True,
                timeout=remaining,
            )
            rows = csv.DictReader(proc.stdout.splitlines(), delimiter="\t")
            page_words: list[str] = []
            for row in rows:
                word = (row.get("text") or "").strip()
                try:
                    confidence = float(row.get("conf") or -1)
                except ValueError:
                    confidence = -1
                if word:
                    page_words.append(word)
                    if confidence >= 0:
                        confidences.append(confidence)
            if page_words:
                texts.append(" ".join(page_words))
                if sum(len(item) for item in texts) >= OCR_MAX_OUTPUT_CHARS:
                    break
        text = "\n".join(texts)
        return text[:OCR_MAX_OUTPUT_CHARS], (
            sum(confidences) / len(confidences) if confidences else None
        )


def assess_agenda_document(
    content: bytes,
    *,
    content_type: str,
    source_url: str,
    ocr_runner=None,
) -> tuple[AgendaTextAssessment, list[DocumentLink]]:
    """Assess one fetched agenda candidate and optionally recover suspicious PDFs with OCR."""
    is_pdf = _is_pdf(content_type, source_url)
    native, discovered = extract_document(content, content_type=content_type, source_url=source_url)
    page_count = _pdf_page_count(content) if is_pdf else 0
    native = native[:AGENDA_TEXT_MAX_CHARS]
    native_alpha = _alpha_chars(native)
    native_score = agenda_content_score(native)
    native_suspicious = (
        _is_placeholder_text(native)
        or native_alpha < 200
        or native_score == 0
        or _repeated_line_ratio(native) > 0.55
    )

    if _is_short_notice(native) and not _is_placeholder_text(native):
        return (
            AgendaTextAssessment(
                native,
                source_url,
                "pdf" if is_pdf else "html",
                "native",
                "accepted",
                "notice",
                "short-notice",
                native_chars=len(native),
                page_count=page_count,
            ),
            discovered,
        )
    if not is_pdf and not native_suspicious:
        return (
            AgendaTextAssessment(
                native,
                source_url,
                "html",
                "native",
                "accepted",
                "agenda",
                "native-quality-pass",
                native_chars=len(native),
            ),
            discovered,
        )
    if is_pdf and not native_suspicious:
        return (
            AgendaTextAssessment(
                native,
                source_url,
                "pdf",
                "native",
                "accepted",
                "agenda",
                "native-quality-pass",
                native_chars=len(native),
                page_count=page_count,
            ),
            discovered,
        )
    if not is_pdf and native_suspicious:
        return (
            AgendaTextAssessment(
                "",
                source_url,
                "html",
                "none",
                "rejected",
                "unknown",
                "placeholder-or-low-quality-html",
                native_chars=len(native),
            ),
            discovered,
        )

    pages = _ocr_page_numbers(page_count)
    runner = ocr_runner or _ocr_pdf_pages
    try:
        probe_text, probe_confidence = runner(content, pages, timeout=OCR_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - convert tool failures into durable diagnostics
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = "ocr-timeout"
        elif isinstance(exc, OcrUnavailableError):
            reason = "ocr-unavailable"
        else:
            reason = "ocr-probe-failed"
        return (
            AgendaTextAssessment(
                "",
                source_url,
                "pdf",
                "none",
                "rejected",
                "unknown",
                reason,
                native_chars=len(native),
                page_count=page_count,
            ),
            discovered,
        )
    probe_score = agenda_content_score(probe_text)
    probe_alpha = _alpha_chars(probe_text)
    if (
        probe_alpha < OCR_MIN_ALPHA_CHARS
        or (probe_confidence is not None and probe_confidence < OCR_MIN_CONFIDENCE)
        or probe_score == 0
    ):
        return (
            AgendaTextAssessment(
                "",
                source_url,
                "pdf",
                "none",
                "rejected",
                "unknown",
                "ambiguous-native-and-ocr",
                native_chars=len(native),
                ocr_chars=len(probe_text),
                page_count=page_count,
                ocr_mean_confidence=probe_confidence,
            ),
            discovered,
        )

    native_similarity = _bounded_text_similarity(native, probe_text)
    materially_better = (
        probe_alpha >= max(OCR_MIN_ALPHA_CHARS, native_alpha * 2) and probe_score > native_score
    )
    if not materially_better and native_suspicious and native_alpha >= OCR_MIN_ALPHA_CHARS:
        materially_better = probe_score > native_score and probe_alpha >= native_alpha * 1.5
    if not materially_better and native_similarity < _SIMILARITY_ACCEPTANCE_THRESHOLD:
        return (
            AgendaTextAssessment(
                "",
                source_url,
                "pdf",
                "none",
                "rejected",
                "unknown",
                "ambiguous-native-and-ocr",
                native_chars=len(native),
                ocr_chars=len(probe_text),
                page_count=page_count,
                native_ocr_similarity=native_similarity,
                ocr_mean_confidence=probe_confidence,
            ),
            discovered,
        )
    if materially_better:
        try:
            full_text, full_confidence = runner(
                content,
                list(range(1, min(page_count, OCR_MAX_PAGES) + 1)),
                timeout=max(
                    OCR_FULL_TIMEOUT_SECONDS,
                    OCR_FULL_SECONDS_PER_PAGE * max(1, min(page_count, OCR_MAX_PAGES)),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - required OCR failure is durable and retryable
            if isinstance(exc, subprocess.TimeoutExpired):
                reason = "ocr-timeout"
            elif isinstance(exc, OcrUnavailableError):
                reason = "ocr-unavailable"
            else:
                reason = "ocr-full-failed"
            return (
                AgendaTextAssessment(
                    "",
                    source_url,
                    "pdf",
                    "none",
                    "rejected",
                    "unknown",
                    reason,
                    native_chars=len(native),
                    ocr_chars=len(probe_text),
                    page_count=page_count,
                    native_ocr_similarity=native_similarity,
                    ocr_mean_confidence=probe_confidence,
                ),
                discovered,
            )
        full_text = full_text[:AGENDA_TEXT_MAX_CHARS]
        full_alpha = _alpha_chars(full_text)
        if (
            full_alpha < OCR_MIN_ALPHA_CHARS
            or (full_confidence is not None and full_confidence < OCR_MIN_CONFIDENCE)
            or agenda_content_score(full_text) <= native_score
        ):
            return (
                AgendaTextAssessment(
                    "",
                    source_url,
                    "pdf",
                    "none",
                    "rejected",
                    "unknown",
                    "ocr-full-quality-failed",
                    native_chars=len(native),
                    ocr_chars=len(full_text),
                    page_count=page_count,
                    native_ocr_similarity=native_similarity,
                    ocr_mean_confidence=full_confidence,
                    truncated=len(full_text) >= AGENDA_TEXT_MAX_CHARS,
                ),
                discovered,
            )
        return (
            AgendaTextAssessment(
                full_text,
                source_url,
                "pdf",
                "ocr",
                "accepted",
                "agenda",
                "ocr-materially-better",
                native_chars=len(native),
                ocr_chars=len(full_text),
                page_count=page_count,
                native_ocr_similarity=native_similarity,
                ocr_mean_confidence=full_confidence,
                truncated=len(full_text) >= AGENDA_TEXT_MAX_CHARS,
            ),
            discovered,
        )
    return (
        AgendaTextAssessment(
            native,
            source_url,
            "pdf",
            "native",
            "accepted",
            "agenda",
            "native-ocr-agreement",
            native_chars=len(native),
            ocr_chars=len(probe_text),
            page_count=page_count,
            native_ocr_similarity=native_similarity,
            ocr_mean_confidence=probe_confidence,
        ),
        discovered,
    )


def extract_pdf_layout_text(content: bytes) -> str:
    """Extract a PDF's visual reading layout using the existing pypdf dependency.

    This is intentionally a lightweight representation rather than a lossy PDF-to-HTML or OCR
    conversion. It preserves indentation/line separation for agenda heading experiments while the
    normal text extractor remains the stable, bounded artifact used elsewhere.
    """
    try:
        from pypdf import PdfReader
        from pypdf.errors import DependencyError, PdfReadError
    except ImportError:
        return content.decode("utf-8", errors="ignore")[:MAX_TEXT_CHARS]
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(
            page.extract_text(extraction_mode="layout") or "" for page in reader.pages
        )[:MAX_TEXT_CHARS]
    except (DependencyError, PdfReadError, TypeError, ValueError, KeyError):
        return _extract_pdf(content)[0]


def _is_pdf(content_type: str, source_url: str | None) -> bool:
    return "pdf" in content_type.lower() or (source_url or "").lower().split("?", 1)[0].endswith(
        ".pdf"
    )


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
    for node in soup.find_all(["script", "style", "nav", "footer", "noscript"]):
        node.decompose()
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
    if _is_pdf(content_type, source_url):
        return extract_pdf_layout_text(content)
    return extract_html_outline(content)


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
    if min(len(left_norm), len(right_norm)) >= 8 and (
        left_norm in right_norm or right_norm in left_norm
    ):
        return 1.0
    return SequenceMatcher(a=left_norm, b=right_norm).ratio()


def _bounded_text_similarity(left: str, right: str) -> float:
    """Compare bounded normalized samples without allowing hostile inputs to consume the run.

    The persisted diagnostic is exact only when both normalized inputs fit the sample bound. When
    their lengths make the acceptance threshold mathematically impossible, the cheap upper bound is
    returned instead of running ``SequenceMatcher``. Otherwise the comparison uses a fixed prefix;
    the quality gate only needs a conservative agreement signal, not a whole-document similarity.
    """
    left_norm = _normalize_ws(left).casefold()
    right_norm = _normalize_ws(right).casefold()
    if not left_norm or not right_norm:
        return 0.0
    upper_bound = 2 * min(len(left_norm), len(right_norm)) / (len(left_norm) + len(right_norm))
    if upper_bound < _SIMILARITY_ACCEPTANCE_THRESHOLD:
        return upper_bound
    return SequenceMatcher(
        a=left_norm[:_SIMILARITY_SAMPLE_CHARS],
        b=right_norm[:_SIMILARITY_SAMPLE_CHARS],
    ).ratio()


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
    is_pdf = _is_pdf(content_type, source_url)
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
