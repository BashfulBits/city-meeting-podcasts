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
        from pypdf.errors import PyPdfError
    except ImportError:
        # pypdf is a required dependency (pyproject.toml declares "pypdf>=5.0"); its absence here
        # means a broken environment, not a malformed document. This used to decode the PDF's own
        # raw bytes as UTF-8 and return that as "extracted text" -- real PDF container syntax and
        # compressed-stream bytes decode into plausible-looking, keyword-bearing noise rather than
        # raising, so it slipped past the quality gate (see the %PDF- guard added to
        # _is_placeholder_text as a second layer) and was persisted as a genuine
        # agenda_text_artifact. Treat this like any other unreadable native PDF instead: return no
        # text so assess_agenda_document's existing suspicious-native/OCR path handles it.
        return "", []
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
    except (TypeError, ValueError, KeyError, PyPdfError):
        # PyPdfError (pypdf.errors.PdfReadError, DependencyError, etc.) previously wasn't caught
        # here, so a genuinely malformed PDF raised straight out of this function instead of
        # degrading gracefully like every other bad-PDF case does. That didn't produce the
        # raw-bytes corruption this module now guards against (the uncaught exception propagated
        # to AgendaTextStage's own per-candidate try/except, which just skips the candidate rather
        # than decoding anything) -- but it also meant a malformed PDF never got the chance at the
        # OCR-repair path below, unlike every other "native extraction is untrustworthy" case.
        #
        # Discard whatever partial text/links were collected before the failure rather than
        # returning them: the failure can land after several pages already appended to `texts`,
        # and if that partial prefix happens to pass the native quality checks on its own,
        # assess_agenda_document would accept a truncated agenda and skip OCR recovery entirely.
        return "", []
    text = "\n".join(texts)[:MAX_TEXT_CHARS]
    return text, _dedupe_links(links + _links_from_text(text, source_url))


_PLACEHOLDER_RE = re.compile(
    r"(?:documentviewer\.php|\bloading(?:\.\.\.|…)?\b|not currently published|"
    r"please wait|javascript (?:is )?required|an error occurred|unable to load)",
    re.I,
)
# The PDF file signature. Real extracted text never starts with this -- only raw, undecoded PDF
# container bytes do (e.g. GH's raw-agenda-bytes bug: a pypdf-import failure silently falling back
# to `content.decode("utf-8", errors="ignore")` on the whole PDF file, see _extract_pdf). That
# leaked binary reads as substantial, keyword-bearing prose to every heuristic below it -- a
# compressed content stream decodes into alpha-heavy noise that easily clears the 200-alpha-char
# and agenda_content_score thresholds by chance -- so it needs its own unambiguous, structural
# signal rather than relying on the score-based checks to catch it.
_RAW_PDF_HEADER_RE = re.compile(r"^\s*%PDF-\d")
_NOTICE_RE = re.compile(
    r"\b(?:cancel(?:led|ed)?|no meeting|meeting (?:is )?(?:postponed|rescheduled)|"
    r"meeting notice|meeting has been canceled)\b",
    re.I,
)
_AGENDA_NUMBER_RE = re.compile(r"(?:^|\n)\s*(?:\d+|[IVXLC]+)(?:\.[A-Z])?\.", re.I)
_GLYPH_TOKEN_SEQUENCE_RE = re.compile(r"(?:/[A-Za-z0-9_-]+[\s\n]*){5,}")
_READABLE_WORD_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}")
_BULLET_PREFIX_RE = re.compile(
    r"^(?:(?:[\u00c5\u212b]\s+)|(?:[•·\uf0b7\u25aa\u25cf\u25cb\u2022\u2023\u2043*–—\-]\s*))+"
)


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


def _is_corrupted_native_text(text: str) -> bool:
    """Detect unreadable native PDF extraction from missing ToUnicode maps or broken fonts.

    PyPDF extracts literal glyph-name tokens (e.g. '/i255', '/1/2/3') or unmapped character
    sequences when a font lacks a valid ToUnicode CMap. This check catches unusable extractions
    while preserving clean multilingual, accented, and legitimate Unicode texts.
    """
    if not text or len(text) < 40:
        return False
    # Strip URLs so legitimate URL path slashes do not trigger slash checks
    no_urls = _URL_RE.sub("", text)
    if not no_urls:
        return False
    # 1. Pervasive PDF glyph-name tokens (e.g. /i255/1/5/6/7)
    if no_urls.count("/") / len(no_urls) > 0.05 or bool(_GLYPH_TOKEN_SEQUENCE_RE.search(no_urls)):
        return True
    # 2. Repeated unmapped replacement characters (U+00FF 'ÿ', U+FFFD) or control bytes
    unmapped_count = text.count("\u00ff") + text.count("\ufffd")
    if unmapped_count >= 5 and (unmapped_count / len(text) > 0.01):
        return True
    if sum(1 for c in text if ord(c) < 32 and c not in "\n\r\t") >= 5:
        return True
    # 3. Garbled character mappings where letters do not form readable words
    alpha_total = _alpha_chars(text)
    if alpha_total >= 200:
        words = _READABLE_WORD_RE.findall(text)
        alpha_in_words = sum(len(w) for w in words)
        if alpha_in_words / alpha_total < 0.50:
            return True
    return False


def _is_placeholder_text(text: str) -> bool:
    normalized = _normalize_ws(text)
    if not normalized:
        return True
    if _RAW_PDF_HEADER_RE.match(normalized):
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


# The publisher itself says there is no agenda ("This agenda is not currently published"), which
# Granicus serves for older meetings that never had one. Nothing is wrong with extraction, so the
# feed-health audit must not ask a human to inspect it.
AGENDA_NOT_PUBLISHED_REASON = "agenda-not-published"
# sha256 of the Granicus AgendaViewer "not currently published" body. Assessments recorded before
# the dedicated reason existed carry this hash with the generic placeholder reason.
_GRANICUS_NOT_PUBLISHED_HASHES = frozenset(
    {"96075776f783fdd68189d7dfa02a8515d5697b6d76b632209342235e2f41e4a1"}
)


_UNPUBLISHED_NOTICE_RE = re.compile(r"\bthis agenda is not currently published\b", re.I)


def _is_publisher_unpublished_notice(text: str) -> bool:
    """The publisher's whole-page notice, not a real agenda that merely uses the phrase."""
    normalized = _normalize_ws(text)
    return len(normalized) <= 500 and bool(_UNPUBLISHED_NOTICE_RE.search(normalized))


def is_unpublished_agenda(assessment: dict | None) -> bool:
    """Whether a stored assessment records the publisher's own "no agenda published" answer."""
    if not isinstance(assessment, dict):
        return False
    reason = assessment.get("reason")
    return reason == AGENDA_NOT_PUBLISHED_REASON or (
        reason == "placeholder-or-low-quality-html"
        and assessment.get("document_hash") in _GRANICUS_NOT_PUBLISHED_HASHES
    )


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
    preserve_lines: bool = False,
) -> tuple[str, float | None]:
    """Render selected PDF pages and return Tesseract text plus mean word confidence.

    By default each page comes back as one space-joined line, which is what agenda assessment was
    built on. ``preserve_lines`` keeps Tesseract's own line breaks, which attendance parsing needs:
    a heading such as "PRESENT:" is only a heading at the start of a line.
    """
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
            lines: dict[tuple[str, str, str], list[str]] = {}
            for row in rows:
                word = (row.get("text") or "").strip()
                try:
                    confidence = float(row.get("conf") or -1)
                except ValueError:
                    confidence = -1
                if word:
                    page_words.append(word)
                    line_key = (
                        row.get("block_num") or "",
                        row.get("par_num") or "",
                        row.get("line_num") or "",
                    )
                    lines.setdefault(line_key, []).append(word)
                    if confidence >= 0:
                        confidences.append(confidence)
            if page_words:
                if preserve_lines:
                    texts.append("\n".join(" ".join(words) for words in lines.values()))
                else:
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
    is_pdf = _is_pdf(content_type, source_url, content)
    native, discovered = extract_document(content, content_type=content_type, source_url=source_url)
    page_count = _pdf_page_count(content) if is_pdf else 0
    native = native[:AGENDA_TEXT_MAX_CHARS]
    native_alpha = _alpha_chars(native)
    native_score = agenda_content_score(native)
    native_corrupted = _is_corrupted_native_text(native)
    native_suspicious = (
        _is_placeholder_text(native)
        or native_corrupted
        or native_alpha < 200
        or native_score == 0
        or _repeated_line_ratio(native) > 0.55
    )

    if _is_short_notice(native) and not _is_placeholder_text(native) and not native_corrupted:
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
                AGENDA_NOT_PUBLISHED_REASON
                if _is_publisher_unpublished_notice(native)
                else "placeholder-or-low-quality-html",
                native_chars=len(native),
            ),
            discovered,
        )

    pages = _ocr_page_numbers(page_count)
    runner = ocr_runner or _ocr_pdf_pages
    try:
        probe_text, probe_confidence = runner(content, pages, timeout=OCR_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - convert tool failures into durable diagnostics
        if native_corrupted:
            reason = "corrupted-native-text"
        elif isinstance(exc, subprocess.TimeoutExpired):
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
                "corrupted-native-text" if native_corrupted else "ambiguous-native-and-ocr",
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
    if not materially_better and native_corrupted:
        # Corrupted native PDF text (e.g. broken font encodings or glyph dumps) cannot be used
        # even if OCR word counts happen to be lower than the corrupted stream's token count.
        materially_better = probe_score > 0 and probe_alpha >= OCR_MIN_ALPHA_CHARS
    if not materially_better and native_similarity < _SIMILARITY_ACCEPTANCE_THRESHOLD:
        return (
            AgendaTextAssessment(
                "",
                source_url,
                "pdf",
                "none",
                "rejected",
                "unknown",
                "corrupted-native-text" if native_corrupted else "ambiguous-native-and-ocr",
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
            if native_corrupted:
                reason = "corrupted-native-text"
            elif isinstance(exc, subprocess.TimeoutExpired):
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
        score_insufficient = (
            agenda_content_score(full_text) == 0
            if native_corrupted
            else agenda_content_score(full_text) <= native_score
        )
        if (
            full_alpha < OCR_MIN_ALPHA_CHARS
            or (full_confidence is not None and full_confidence < OCR_MIN_CONFIDENCE)
            or score_insufficient
        ):
            return (
                AgendaTextAssessment(
                    "",
                    source_url,
                    "pdf",
                    "none",
                    "rejected",
                    "unknown",
                    "corrupted-native-text" if native_corrupted else "ocr-full-quality-failed",
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
    if native_corrupted:
        return (
            AgendaTextAssessment(
                "",
                source_url,
                "pdf",
                "none",
                "rejected",
                "unknown",
                "corrupted-native-text",
                native_chars=len(native),
                ocr_chars=len(probe_text),
                page_count=page_count,
                native_ocr_similarity=native_similarity,
                ocr_mean_confidence=probe_confidence,
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
        # Same reasoning as _extract_pdf's ImportError branch: a missing pypdf is a broken
        # environment, not a malformed document, and raw PDF bytes must never be handed back as
        # if they were extracted text.
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(
            page.extract_text(extraction_mode="layout") or "" for page in reader.pages
        )[:MAX_TEXT_CHARS]
    except (DependencyError, PdfReadError, TypeError, ValueError, KeyError):
        return _extract_pdf(content)[0]


def _is_pdf(content_type: str, source_url: str | None, content: bytes | None = None) -> bool:
    """PDF by content type, URL suffix, or -- when ``content`` is given -- the file signature.

    Some portals serve a real PDF as ``application/octet-stream`` from an extensionless URL
    (swagit ``/videos/N/agenda``); without the byte check those fall into the HTML path and are
    rejected as placeholder-or-low-quality-html."""
    return (
        "pdf" in content_type.lower()
        or (source_url or "").lower().split("?", 1)[0].endswith(".pdf")
        or (content is not None and content.lstrip()[:5] == b"%PDF-")
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
    if _is_pdf(content_type, source_url, content):
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
        line = _BULLET_PREFIX_RE.sub("", line).strip()
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
            clean_title = _BULLET_PREFIX_RE.sub("", numbered.group("title")).strip()
            add(f"{prefix} {clean_title}", line_number)
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
    is_pdf = _is_pdf(content_type, source_url, content)
    if is_pdf:
        return _extract_pdf(content, source_url=source_url)
    return extract_html(content, source_url)


# Scanned minutes carry no text layer. OCR text is only trusted when Tesseract itself is confident:
# a roster built from misread names would act as a correction constraint and suppress the right
# speaker matches, which is worse than no roster.
OCR_MINUTES_MIN_CONFIDENCE = 85.0
# Real minutes carry well over a thousand letters a page. A scan whose only selectable text is a
# running header ("City Council Meeting Minutes" on every page) clears the flat letter floor, so
# sparseness is also judged per page and by how much of the text is the same line repeated.
OCR_MIN_ALPHA_PER_PAGE = 150
_SPARSE_REPEATED_LINE_RATIO = 0.5


def minutes_text_is_sparse(text: str, page_count: int | None = None) -> bool:
    """Whether extracted minutes text is a scan's stray text layer rather than the document."""
    alpha = _alpha_chars(text)
    if alpha < OCR_MIN_ALPHA_CHARS:
        return True
    if page_count and alpha / page_count < OCR_MIN_ALPHA_PER_PAGE:
        return True
    return _repeated_line_ratio(text) > _SPARSE_REPEATED_LINE_RATIO


def extract_minutes_document(
    content: bytes,
    *,
    content_type: str = "",
    source_url: str | None = None,
    ocr_runner=None,
    ocr_allowed=None,
) -> tuple[str, list[DocumentLink], str]:
    """Extract minutes text, falling back to OCR for a scanned PDF with no text layer.

    Returns ``(text, links, method)``. ``method`` is ``"native"`` or ``"ocr"`` for text,
    ``"none"`` when the document is deliberately empty or OCR was not confident enough, and two
    outcomes that must **not** be cached as empty: ``"retry"`` (Tesseract missing, timed out or
    failed -- a later run may succeed) and ``"deferred"`` (``ocr_allowed`` said the run's budget
    is spent, so the expensive scan was not started).

    OCR is attempted only when a PDF's native text is nearly empty, runs over at most
    ``OCR_MAX_PAGES`` pages, and is discarded as ``"none"`` when its mean confidence is under
    ``OCR_MINUTES_MIN_CONFIDENCE``. Rendering and recognition share one deadline for the whole
    document, sized from the page count (``OCR_FULL_SECONDS_PER_PAGE`` each); it is not enforced
    page by page.
    """
    native, links = extract_document(content, content_type=content_type, source_url=source_url)
    empty_method = "native" if native.strip() else "none"
    if not _is_pdf(content_type, source_url, content):
        return native, links, empty_method
    page_count = _pdf_page_count(content)
    if page_count <= 0 or not minutes_text_is_sparse(native, page_count):
        return native, links, empty_method
    if ocr_allowed is not None and not ocr_allowed():
        return native, links, "deferred"
    pages = list(range(1, min(page_count, OCR_MAX_PAGES) + 1))
    runner = ocr_runner or _ocr_pdf_pages
    try:
        text, confidence = runner(
            content,
            pages,
            timeout=max(OCR_FULL_TIMEOUT_SECONDS, OCR_FULL_SECONDS_PER_PAGE * len(pages)),
            preserve_lines=True,
        )
    except (OcrUnavailableError, subprocess.SubprocessError, OSError):
        return native, links, "retry"
    if (
        confidence is None
        or confidence < OCR_MINUTES_MIN_CONFIDENCE
        or _alpha_chars(text) < OCR_MIN_ALPHA_CHARS
    ):
        return native, links, empty_method
    if native.strip() and parse_roster(native) and not parse_roster(text):
        # A scan can have a selectable attendance line over an image-only body. OCR read the body
        # but missed that line, so keep the native roster and append the OCR'd body.
        return f"{native.rstrip()}\n{text}", links, "ocr"
    return text, links, "ocr"


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


# An optional qualifier may precede the status word ("MEMBERS PRESENT:", "STAFF PRESENT:"). It is
# what separates a council member from the City Attorney on the attendance list, and capturing it
# also *widens* extraction: with the status word previously anchored to the start of the line, the
# cities that helpfully label their sections were exactly the ones whose rosters parsed as empty.
_ATTENDANCE_STATUS_RE = re.compile(
    # An optional leading line number: some minutes PDFs carry printed line numbers ("10 PRESENT:").
    r"^[ \t]*(?:\d{1,3}[ \t]+)?(?P<qualifier>[A-Za-z][A-Za-z /&'-]{0,40}?)?[ \t]*"
    r"(?P<status>present|absent|excused|recused)"
    # "PRESENT IN PERSON:", "PRESENT VIRTUALLY:" -- how the member attended, not who they are.
    r"(?:[ \t]+(?:in[ \t]+person|virtually|remotely|via[ \t]+[A-Za-z]{3,12}))?"
    r"[ \t]*:[ \t]*(?P<names>.*)$",
    re.I | re.M,
)
_ATTENDANCE_SPLIT_RE = re.compile(r",|;|\s{2,}|\s+(?:and|&)\s+")
_ATTENDANCE_SPLIT_CAPTURE_RE = re.compile(r"(,|;|\s{2,}|\s+(?:and|&)\s+)")
_WHITESPACE_RE = re.compile(r"\s+")

# Qualifier vocabulary, matched on any contained word so "Council Members", "Board Members" and
# "City Staff" all resolve. Emitted canonically ("members"/"staff") rather than as raw text, so
# `citypods.speakers.classify_speaker_tier` does not have to re-derive one city's phrasing.
_ROSTER_MEMBER_WORDS = frozenset(
    {
        "member",
        "members",
        "councilmember",
        "councilmembers",
        "council",
        "board",
        "commission",
        "commissioners",
        "trustees",
        "aldermen",
        "supervisors",
        "directors",
    }
)
_ROSTER_STAFF_WORDS = frozenset(
    {
        "staff",
        "administration",
        "personnel",
        "employees",
        # "ALSO PRESENT:" is the conventional heading for the clerk, attorney and manager -- the
        # people present who are *not* on the body. It matched no vocabulary, so it fell through
        # as an unsectioned row, and an unsectioned roster hit tiers as `member`: the City
        # Manager would have been handed the cross-meeting speaker page §C.4.1 withholds.
        "also",
        "additionally",
    }
)
# Attendance lines that record who was in the *room*, not who serves the body. These must not
# enter the roster at all: it seeds the person registry and acts as a correction constraint, so an
# audience member landing there would be enrolled as a probable official and -- because an
# unsectioned roster hit tiers as `member` -- could be offered a cross-meeting speaker page.
# Anchoring the status word to the start of the line used to exclude these by accident; widening
# the pattern to capture section qualifiers means they now have to be excluded on purpose.
_ROSTER_EXCLUDED_WORDS = frozenset(
    {
        "others",
        "other",
        "guests",
        "guest",
        "public",
        "visitors",
        "audience",
        "applicants",
        "applicant",
        "petitioners",
        "speakers",
    }
)


def _roster_section(qualifier: str | None) -> str | None:
    """Map an attendance-line qualifier to a canonical roster section, or None if unclear.

    Silence beats a guess: an unrecognized qualifier leaves the row unsectioned, which
    `classify_speaker_tier` already handles by falling back to spoken-title vocabulary.
    """
    words = _qualifier_words(qualifier)
    if not words:
        return None
    # Staff wins a mixed qualifier ("Staff Members Present"): "members" is generic filler there,
    # while "staff" is the discriminating word. Misfiling a staffer as a member would hand them a
    # cross-meeting speaker page the tier policy deliberately withholds (review/31 §C.4.1).
    if words & _ROSTER_STAFF_WORDS:
        return "staff"
    if words & _ROSTER_MEMBER_WORDS:
        return "members"
    return None


def _qualifier_words(qualifier: str | None) -> set[str]:
    return {word for word in _WHITESPACE_RE.split(_norm_qualifier(qualifier)) if word}


def _norm_qualifier(value: str | None) -> str:
    return re.sub(r"[^a-z\s]", " ", str(value or "").casefold())


# Leading honorifics/offices to strip, longest first so "Mayor Pro Tem" beats "Mayor". Required
# for corroboration to work at all: the roster name has to match the *spoken* name, so a stored
# "Mayor Gerard Hudspeth" would never corroborate a chair cue proposing "Gerard Hudspeth" -- and
# it would fail for exactly the people who speak most.
_ROSTER_NAME_TITLES: tuple[tuple[str, ...], ...] = tuple(
    sorted(
        (
            ("chair", "pro", "tem"),
            ("chair", "pro", "tern"),
            ("members",),
            ("member",),
            ("mayor", "pro", "tem"),
            # OCR/scan variant that shows up in real minutes ("Mayor Pro Tern Paul Meltzer").
            ("mayor", "pro", "tern"),
            ("deputy", "mayor"),
            ("vice", "mayor"),
            ("vice", "chair"),
            ("council", "member"),
            ("council", "person"),
            ("council", "man"),
            ("council", "woman"),
            ("councilmember",),
            ("councilperson",),
            ("councilman",),
            ("councilwoman",),
            ("councilor",),
            ("councillor",),
            ("commissioner",),
            ("alderman",),
            ("alderwoman",),
            ("alderperson",),
            ("selectman",),
            ("selectwoman",),
            ("selectperson",),
            ("freeholder",),
            ("assemblyman",),
            ("assemblywoman",),
            ("assemblymember",),
            ("trustee",),
            ("supervisor",),
            ("representative",),
            ("senator",),
            ("delegate",),
            ("chairman",),
            ("chairwoman",),
            ("chairperson",),
            ("chair",),
            ("mayor",),
            ("dr",),
            ("mr",),
            ("ms",),
            ("mrs",),
        ),
        key=len,
        reverse=True,
    )
)

# Tokens that prove a captured span is prose or parliamentary boilerplate rather than a name.
# An attendance line is not guaranteed to hold names at all ("Present: a quorum was established",
# "Absent: None"), and what it holds flows straight into the person registry.
_NON_NAME_TOKENS = frozenset(
    {
        "a",
        "an",
        "absent",
        "all",
        "and",
        "at",
        "established",
        "excused",
        "held",
        "is",
        "meeting",
        "none",
        "no",
        "not",
        "of",
        "present",
        "quorum",
        "recused",
        "the",
        "there",
        "total",
        "vacant",
        "was",
        "were",
        "yes",
    }
)
# Role and modifier words that can lead a staff title. Stripped from the front of a roster entry
# one word at a time (see `_clean_roster_name`), which covers the open-ended real phrasings --
# "City Manager", "Interim Assistant City Manager", "Deputy Chief of Police" -- without trying to
# enumerate them. Kept separate from `_ROSTER_NAME_TITLES`, which holds whole elected offices.
_STAFF_ROLE_WORDS = frozenset(
    {
        "acting",
        "administrator",
        "analyst",
        "assistant",
        "associate",
        "attorney",
        "chief",
        "city",
        "clerk",
        "coordinator",
        "county",
        "deputy",
        "director",
        "engineer",
        "executive",
        "general",
        "interim",
        "manager",
        "officer",
        "planner",
        "planning",
        "secretary",
        "senior",
        "specialist",
        "superintendent",
        "town",
        "village",
    }
)
# A span made only of these is an office or suffix left over from splitting a wrapped line
# ("... Mayor Pro Tem Kamp and Council" / "Members ..."), not a person.
_GENERIC_ROSTER_WORDS = frozenset(
    {
        "council",
        "member",
        "members",
        "mayor",
        "pro",
        "tem",
        "tern",
        "chair",
        "vice",
        "board",
        "committee",
        "commission",
        "commissioner",
        "commissioners",
        "jr",
        "sr",
        "ii",
        "iii",
        "iv",
    }
)
_NAME_PARTICLES = frozenset({"de", "del", "la", "le", "van", "von", "da", "di", "st", "mc", "mac"})
_MAX_ROSTER_NAME_WORDS = 5
_MAX_ROSTER_NAME_CHARS = 60


# "Vice-Chair" and the OCR'd "Vice- Chair" are the same title as "Vice Chair".
_VICE_CHAIR_HYPHEN_RE = re.compile(r"\bvice\s*-\s*chair\b", re.I)


def _clean_roster_name(raw: str) -> str | None:
    """Return a bare person name, or ``None`` when the span is not name-shaped.

    Silence beats a guess here by a wide margin. A roster name is not merely displayed: it
    enrols a person in the body registry, is carried forward as standing membership, tiers as a
    **member** (the tier that earns a speaker page), and — through `roster_person_ids` — acts as a
    *correction constraint* that removes voice matches outside it. A single junk entry therefore
    suppresses correct naming for the whole meeting, which is strictly worse than extracting
    nothing: an empty roster already means "make no correction".
    """
    name = _VICE_CHAIR_HYPHEN_RE.sub("Vice Chair", _WHITESPACE_RE.sub(" ", raw)).strip(" .,:;")
    if not name or len(name) > _MAX_ROSTER_NAME_CHARS or any(char.isdigit() for char in name):
        return None
    words = name.split(" ")
    while words:
        lowered = tuple(word.casefold().strip(".") for word in words)
        for title in _ROSTER_NAME_TITLES:
            if lowered[: len(title)] == title:
                words = words[len(title) :]
                break
        else:
            # Staff titles are open-ended phrases ("City Manager", "Interim Assistant Planning
            # Director"), so they are stripped word-by-word from the front rather than enumerated.
            # Leading-only, so a surname that happens to be a role word ("Mark Manager") survives.
            # Without this an "ALSO PRESENT:" line enrols the *title* as part of the person's
            # name, which then matches no spoken name and corroborates nothing.
            if len(words) > 1 and lowered[0] in _STAFF_ROLE_WORDS:
                words = words[1:]
                continue
            break
    if not 0 < len(words) <= _MAX_ROSTER_NAME_WORDS:
        return None
    if any(word.casefold().strip(".") in _NON_NAME_TOKENS for word in words):
        return None
    # An office listed with no name at all ("ALSO PRESENT: City Manager, City Attorney").
    # Leading-only stripping always spares the last word, so this would otherwise enrol people
    # called "Manager" and "Attorney" -- who then narrow `roster_person_ids` and remove correct
    # voice matches for the whole meeting. "Mark Manager" survives: "mark" is not a role word.
    if all(word.casefold().strip(".") in _STAFF_ROLE_WORDS for word in words):
        return None
    cleaned = " ".join(words).strip(" .,:;")
    if len(cleaned) < 2 or not any(char.isalpha() for char in cleaned):
        return None
    if all(word.casefold().strip(".,") in _GENERIC_ROSTER_WORDS for word in words):
        return None
    return cleaned


# "PRESENT: Chair A, Commissioners: B, C" -- a sub-label inside one line, not part of a name.
_INLINE_LABEL_RE = re.compile(
    r"\b(?:commissioners?|(?:board|council|committee|commission)\s+members?|members?)\s*:\s*",
    re.I,
)
_ROSTER_BLOCK_STOP_RE = re.compile(
    r"^[ \t]*(?:\d{1,3}[ \t]*[.)]?[ \t]+)?(?:[A-Za-z /&'-]{0,40}?)"
    r"(?:present|absent|excused|recused)\b[^\n]*:[ \t]*|^[ \t]*\d{1,3}[.)][ \t]|[^\n]*:[ \t]*$",
    re.I,
)
_MAX_ROSTER_CONTINUATION_LINES = 30
_HEADING_LOOKBACK_CHARS = 400
_HEADING_LOOKAHEAD_CHARS = 4000
# Section headings that follow an attendance block. A line that carries one ends the block, so
# "PUBLIC COMMENT" is never filed as a person.
_SECTION_HEADING_RE = re.compile(
    r"\b(?:public\s+comments?|citizens?\s+comments?|call\s+to\s+order|pledge|invocation|"
    r"agenda|minutes|approval|consent|regular\s+meeting|work\s+session|adjourn\w*|"
    r"announcements?|presentations?|new\s+business|old\s+business|executive\s+session|"
    r"roll\s+call|reports?|discussion|items?\s+for|recess)\b",
    re.I,
)


def _follows_heading(text: str, start: int) -> bool:
    """Whether the line above this one is itself an attendance heading with no names.

    "PRESENT IN PERSON:" / "PRESENT VIRTUALLY:" / "ABSENT:" followed by one line of names per
    heading is a flattened table: the names under the last heading belong to the first, not to it.
    """
    previous = text[max(0, start - _HEADING_LOOKBACK_CHARS) : start].rstrip().rsplit("\n", 1)[-1]
    heading = _ATTENDANCE_STATUS_RE.match(previous.strip())
    return bool(heading and not heading.group("names").strip())


_WRAP_STOP_WORDS = frozenset({"also", "staff", "city", "others", "guests", "excused", "none"})
_NO_ATTENDANCE_RE = re.compile(r"^(?:none|n/a|no\s+one|nobody|vacant)\.?$", re.I)


_SURNAME_LIST_START_RE = re.compile(r"^[A-Z][A-Za-z'\u2019-]+(?:,|\s+and\b)")
_SHORT_NAME_TAIL_RE = re.compile(
    r"^(?:[A-Z][A-Za-z'\u2019.-]+)(?:\s+[A-Z][A-Za-z'\u2019.-]+)?[,.]?$"
)
_LIST_OPEN_END_RE = re.compile(r"(?:[,;&]|\band)\s*$", re.I)


def _continues_a_wrapped_list(previous: str, line: str) -> bool:
    """Whether ``line`` carries on a name list that ``previous`` left unfinished.

    Only two shapes qualify: the previous line ends mid-list (a comma, "and", "&"), or this line
    opens like the tail of a list (a surname then a comma, or a lone short name). Anything else
    -- a section heading ("WORK SESSION"), prose, a numbered item, "Also present ..." -- is the
    next part of the minutes, and appending it would turn it into names.
    """
    if len(line) > 120 or line.split(" ", 1)[0].casefold() in _WRAP_STOP_WORDS:
        return False
    if _LIST_OPEN_END_RE.search(previous):
        return bool(line[:1].isupper())
    if line.isupper():
        return False
    return _wraps_onto(previous, line)


def _wraps_onto(previous: str, line: str) -> bool:
    """Evidence that ``line`` finishes an entry ``previous`` started, rather than starting its own.

    A surname then a comma ("Briggs, Jesse Davis"), or a lone short name straight after a title
    word ("Council Members Deb" / "Armintor"). A bare title-case line after a complete name
    ("Alice Smith" / "Public Comment") is the next part of the minutes, not the rest of a name.
    """
    if _SURNAME_LIST_START_RE.match(line):
        return True
    words = previous.replace(",", " ").split()
    return bool(
        _SHORT_NAME_TAIL_RE.match(line)
        and len(words) >= 2
        and words[-2].casefold().strip(".") in _GENERIC_ROSTER_WORDS
    )


_PRINTED_LINE_NUMBER_RE = re.compile(r"^\d{1,3}\s+(?=\S)")
_ABBREVIATED_END_RE = re.compile(r"(?:\b[A-Z]|\bJr|\bSr|\bDr|\bMr|\bMs|\bMrs)\.$")


def _ends_a_sentence(line: str) -> bool:
    """A closing period, but not the one on a suffix or initial ("Alice Smith Jr.", "Robert J.")."""
    return line.endswith(".") and not _ABBREVIATED_END_RE.search(line)


def _names_after_heading(
    text: str, end: int, *, wrapped: bool = False, previous: str = ""
) -> tuple[str, int] | None:
    """Names a heading like ``PRESENT:`` leaves for the lines below it.

    Returns ``(names, end_offset)`` -- the names and where the last line used ends in ``text``, so
    the row's evidence can show them -- or None when the list is cut off and must be rejected.

    Only a single heading followed by its own names qualifies. Minutes that print every heading
    first ("PRESENT:", "ABSENT:", then one line of names per heading) put the names in an order
    that cannot be matched to headings by position alone, so a second heading right below this
    one yields nothing: silence beats a guess.
    """
    names: list[str] = []
    joined = ""
    lookahead = text[end : end + _HEADING_LOOKAHEAD_CHARS]
    lines = lookahead.split("\n")
    clipped = end + _HEADING_LOOKAHEAD_CHARS < len(text)
    if clipped:
        lines = lines[:-1]  # the last line was cut mid-way and could be a half-read name
    ended = False
    consumed_end = end
    cursor = end + len(lines[0]) + 1
    for line in lines[1 : 1 + _MAX_ROSTER_CONTINUATION_LINES + 2]:
        line_end = cursor + len(line)
        cursor = line_end + 1
        # Minutes with printed line numbers number every line ("11 Alice Smith").
        stripped = _PRINTED_LINE_NUMBER_RE.sub("", line.strip())
        if not stripped:
            ended = True
            break
        if (
            _ROSTER_BLOCK_STOP_RE.match(line.strip())
            or _SECTION_HEADING_RE.search(stripped)
            or _NO_ATTENDANCE_RE.match(stripped)
        ):
            ended = True  # "None" says nobody is listed, so the next line is not a name
            break
        # A change from mixed case to ALL CAPS is a new section, not another member.
        if stripped.isupper() and any(any(c.islower() for c in earlier) for earlier in names):
            ended = True
            break
        before = names[-1] if names else previous
        if wrapped and not _continues_a_wrapped_list(before, stripped):
            ended = True
            break
        if len(names) >= _MAX_ROSTER_CONTINUATION_LINES:
            # More names follow than the bound allows: publishing the first few as the whole
            # roster would exclude the rest from speaker correction. None tells the caller to
            # reject the whole heading, which is not the same as "no continuation".
            return None
        if names:
            # Lines are separate entries unless one visibly wraps onto the next: joining
            # "ALICE SMITH" and "BOB JONES" with a space would make one four-word name.
            joiner = (
                " " if _LIST_OPEN_END_RE.search(before) or _wraps_onto(before, stripped) else ", "
            )
            joined += joiner
        joined += stripped
        names.append(stripped)
        consumed_end = line_end
        if _ends_a_sentence(stripped):
            ended = True
            break
    else:
        # Ran out of lines without reaching an end. If the text was clipped, the list may go on.
        if clipped and not ended:
            return None
    return joined, consumed_end


_PLURAL_ROSTER_TITLE_RE = re.compile(
    r"\b(?:council\s*members?|commissioners?|(?:committee|board|commission)\s+members?|"
    r"trustees|directors|aldermen)\b",
    re.I,
)
_DASH_ROLE_SUFFIX_RE = re.compile(
    r"\s*[\u2013\u2014-]\s*(?:(?:vice|co)[- ]?)?(?:chair(?:man|woman|person)?|secretary|treasurer)"
    r"\s*$",
    re.I,
)
# A colon is not a boundary: "Staff: A and B were in attendance" must keep its label. A period
# after an initial or title abbreviation ("Robert J. Van Dyke", "Mr. Metayer") is not one either.
_NOT_AFTER_ABBREVIATION = (
    r"(?<!\b[A-Z]\.)(?<!\bJr\.)(?<!\bSr\.)(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)"
)
_SENTENCE_START_RE = re.compile(rf"(?:^|{_NOT_AFTER_ABBREVIATION}(?<=\.)\s+(?=[A-Z]))")
# Text up to the end of a sentence: any character but a period, except the period of an initial
# or title abbreviation, so a name like "Robert J. Van Dyke" is not cut in half.
_SPAN_CHAR = (
    r"(?:[^.]|(?<=\b[A-Z])\.|(?<=\bJr)\.|(?<=\bSr)\.|(?<=\bDr)\.|(?<=\bMr)\.|(?<=\bMs)\.|"
    r"(?<=\bMrs)\.)"
)
# "were present to give public comment" names visitors, not members: a purpose clause disqualifies.
_PROSE_PRESENT_RE = re.compile(
    rf"(?P<names>{_SPAN_CHAR}{{3,500}}?)\s+(?:was|were)\s+(?:in\s+attendance|present)\b"
    r"(?!\s+(?:to|for|during|at\s+the\s+(?:podium|microphone)))",
    re.I,
)
# Words that show a sentence is about members of the body rather than whoever happened to be there.
_MEMBER_EVIDENCE_WORDS = _ROSTER_MEMBER_WORDS | frozenset(
    {"mayor", "chair", "vice", "commissioner", "tem", "councilmember", "councilmembers"}
)
_PROSE_ABSENT_RE = re.compile(
    rf"(?P<names>{_SPAN_CHAR}{{3,500}}?)\s+(?:was|were)\s+(?P<phrase>absent|not\s+present|excused)\b",
    re.I,
)
# "Mayor Jane Doe called the regular meeting to order": the presiding member is present and, being
# the one who speaks most, the person a roster most needs to hold.
_PRESIDER_RE = re.compile(
    rf"(?P<names>{_SPAN_CHAR}{{3,120}}?)\s+called\s+(?:the\s+)?(?:[A-Za-z]+\s+){{0,3}}?"
    r"(?:meeting|work\s*session|session)\s+to\s+order\b",
    re.I,
)
_PRESIDER_TITLE_WORDS = frozenset(
    {"mayor", "chair", "chairman", "chairwoman", "chairperson", "vice", "commissioner"}
)
_ENCLOSING_NON_MEMBER_LABEL_RE = re.compile(
    r"\b(?:guests?|visitors?|audience|public|others?|staff|applicants?|speakers?)\s*:\s*$", re.I
)
_QUALIFIED_ATTENDANCE_RE = re.compile(
    r"(?P<qualifier>[A-Z][A-Za-z ]{2,40}?)\s+in\s+attendance(?:\s+include)?\s*:?\s+"
    rf"(?P<names>{_SPAN_CHAR}{{3,500}})",
)
# A run-on label ("... Lisa Ely Commissioners absent: ...") ends a name list without punctuation.
_RUN_ON_LABEL_RE = re.compile(
    r"\s+(?:commissioners?|members?|staff|city|absent|also|guests?)\b.*$", re.I | re.S
)


def _looks_like_prose_name(name: str) -> bool:
    """Prose is full of capitalised-or-not words, so a prose name must look like a name.

    Every word is capitalised (or a name particle) and at least two words long: the prose lists
    this reads give full names, and a lone lowercase or single word is far more likely to be a
    fragment ("seperately", "com Wednesday") than a person.
    """
    words = name.split()
    if len(words) < 2 or any(char in name for char in "()$@/"):
        return False
    last = words[-1].casefold().strip(".")
    if last in _STAFF_ROLE_WORDS | {"president", "vice"} and (
        len(words) > 2 or words[-2].casefold().strip(".") in _STAFF_ROLE_WORDS
    ):
        return False  # "PCDC Executive Director": an office, not a person ("Mark Manager" is)
    return all(w[0].isupper() or w.casefold().strip(".") in _NAME_PARTICLES for w in words)


_STAFF_ROLE_NOUNS = frozenset(
    {
        "administrator",
        "analyst",
        "attorney",
        "chief",
        "clerk",
        "coordinator",
        "director",
        "engineer",
        "manager",
        "officer",
        "planner",
        "secretary",
        "specialist",
        "superintendent",
    }
)


_STAFF_DEPARTMENT_WORDS = frozenset(
    {
        "airport", "animal", "budget", "building", "code", "communications", "community",
        "compliance", "court", "development", "economic", "emergency", "engineering", "finance",
        "fire", "human", "information", "legal", "library", "management", "municipal",
        "parks", "planning", "police", "public", "recreation", "resources", "services",
        "shelter", "technology", "transportation", "utilities", "water", "works",
    }
)  # fmt: skip


def _is_staff_title_phrase(part: str) -> bool:
    """Whether a span after a comma is a job title ("Parks Director", "PCDC Executive Director").

    Needs a role noun and nothing but role words, department words or an acronym around it. A
    full name that happens to end in a role word ("Mark Manager") is not one: "Mark" is neither.
    """
    original = part.split()
    words = [w.casefold().strip(".") for w in original]
    if not words or not (_STAFF_ROLE_NOUNS & set(words)):
        return False
    return all(
        w in _STAFF_ROLE_WORDS or w in _STAFF_DEPARTMENT_WORDS or (o.isupper() and len(o) >= 2)
        for w, o in zip(words, original, strict=True)
    )


def _has_leading_staff_title(words: list[str]) -> bool:
    """Whether an entry opens with a staff title ("City Attorney Bob Roe", "Parks Director X").

    Cleaning strips leading titles to reach the name, so a staffer would otherwise come out as a
    plain member. Only a title containing a role noun counts: "City Councilmember Wayne Cooper"
    and "County Commissioner Jane Doe" lead with "city"/"county" but name members, and a surname
    such as "Mark Manager" does not open with a title at all.
    """
    consumed: list[str] = []
    for word in words:
        if word not in _STAFF_ROLE_WORDS and word not in _STAFF_DEPARTMENT_WORDS:
            break
        consumed.append(word)
    return bool(consumed) and len(consumed) < len(words) and bool(_STAFF_ROLE_NOUNS & set(consumed))


def _prose_roster_rows(names_text: str, status: str, section: str | None, evidence: str) -> list:
    """Rows for a prose list like "Mayor Pro Tem A and Councilmembers B, C and D".

    A person followed by a pure staff title ("Charles Zech, City Attorney") is a staffer who
    shares the sentence with the members, so both are dropped rather than filed as a member.
    """
    # Prose is already single-spaced, so the gap left by a removed title must be closed or it would
    # read as a column break and split "City Councilmembers Wayne Cooper" into "City" and a name.
    names_text = _WHITESPACE_RE.sub(" ", _PLURAL_ROSTER_TITLE_RE.sub(" ", names_text))
    pieces = _ATTENDANCE_SPLIT_CAPTURE_RE.split(names_text)
    kept: list[str | None] = []
    for index in range(0, len(pieces), 2):
        part = _DASH_ROLE_SUFFIX_RE.sub("", pieces[index])
        after_comma = index > 0 and pieces[index - 1].strip() in {",", ";"}
        words = [w.casefold().strip(".") for w in _WHITESPACE_RE.sub(" ", part).split() if w]
        # A title that follows a name after a comma ("Charles Zech, City Attorney", "A, PCDC
        # Executive Director") belongs to that name's owner, who is staff, not a member. Joined
        # with "and" the same words can be a surname ("Mark Manager"), so only a comma counts.
        if _has_leading_staff_title(words):
            kept.append(
                None
            )  # "City Attorney Bob Roe": cleaning would strip the title, not the role
            continue
        is_title = bool(words) and (
            all(w in _STAFF_ROLE_WORDS for w in words)
            or (after_comma and _is_staff_title_phrase(part))
        )
        if is_title:
            if kept:
                kept[-1] = None  # the name just before a staff title is that staffer
            continue
        kept.append(_clean_roster_name(part))
    rows = []
    for name in kept:
        if not name or not _looks_like_prose_name(name):
            continue
        row = {"name": name, "status": status, "evidence": evidence[:500]}
        if section:
            row["section"] = section
        rows.append(row)
    return rows


# A call-to-order heading or convening statement, not any "to order" ("a motion to order supplies").
_CALL_TO_ORDER_RE = re.compile(
    r"\bcall(?:ed)?\s+(?:(?:the\s+)?(?:[A-Za-z]+\s+){0,3})?to\s+order\b", re.I
)
_PROSE_WINDOW_BEFORE = 400
_PROSE_WINDOW_AFTER = 2500


def _opening_window(flat: str) -> str:
    """The stretch of minutes where the body's own attendance is recorded.

    "... were in attendance" also introduces presenters, applicants and guests deep in the item
    discussion, and those are not members. Attendance is recorded around the call to order, so
    only that window is read; without a call to order the document's opening is used.
    """
    match = _CALL_TO_ORDER_RE.search(flat)
    if match is None:
        return flat[:_PROSE_WINDOW_AFTER]
    start = max(0, match.start() - _PROSE_WINDOW_BEFORE)
    return flat[start : match.end() + _PROSE_WINDOW_AFTER]


def _non_member_lead(sentence: str) -> bool:
    """Whether a sentence is led by a staff or guest label ("Staff:", "Guests Jane Doe ...").

    The label is read before it is stripped, with or without a colon, so a staff or guest list is
    never admitted as members; "Commission Members:" and "Commissioners Jane Doe" still are.
    """
    lead = sentence.split(":")[0] if ":" in sentence else " ".join(sentence.split(" ")[:3])
    return bool(_qualifier_words(lead) & (_ROSTER_STAFF_WORDS | _ROSTER_EXCLUDED_WORDS | {"staff"}))


def _member_sentence(span: str, *, require_member_evidence: bool = False) -> str | None:
    """The member list a matched span ends in, or None when it is a staff/guest list.

    Trims to the span's own sentence, refuses a staff or guest label, then drops a harmless
    "Commission Members:" label so only the names remain.
    """
    sentence = _SENTENCE_START_RE.split(span)[-1].strip()
    if _non_member_lead(sentence):
        return None
    if require_member_evidence and not _qualifier_words(sentence) & _MEMBER_EVIDENCE_WORDS:
        return None  # presence alone is not membership; the label counts as evidence
    if ":" in sentence:
        sentence = sentence.rsplit(":", 1)[1].strip()
    if _qualifier_words(sentence) & {"staff", "employees", "attending"}:
        return None
    return sentence


def _roster_key(name: str) -> str:
    """Dedupe key that keeps diacritics: "Jose" and "José" are different people on a roster."""
    return " ".join(name.casefold().split())


def _parse_prose_attendance(text: str) -> list[dict]:
    """Members named in attendance sentences ("... were in attendance", "... in attendance: A, B").

    Staff lists are skipped: their "Name, Title; Name, Title" shape cannot be told from a list of
    members without a title vocabulary, and a staffer filed as a member would be handed the
    cross-meeting speaker page the tier policy withholds (review/31 section C.4.1).
    """
    flat = _opening_window(_WHITESPACE_RE.sub(" ", text))
    rows: list[dict] = []
    for pattern in (_PROSE_PRESENT_RE, _PROSE_ABSENT_RE):
        for match in pattern.finditer(flat):
            status = "present"
            if pattern is _PROSE_ABSENT_RE:
                status = "excused" if match.group("phrase").casefold() == "excused" else "absent"
            # The match is lazy from wherever scanning resumed, so trim to its own sentence.
            sentence = _member_sentence(match.group("names"), require_member_evidence=True)
            if sentence is None:
                continue
            rows += _prose_roster_rows(sentence, status, None, match.group(0))
    presider = _PRESIDER_RE.search(flat)
    if presider is not None:
        sentence = _member_sentence(presider.group("names"))
        if sentence is not None and _qualifier_words(sentence) & _PRESIDER_TITLE_WORDS:
            rows += _prose_roster_rows(sentence, "present", None, presider.group(0))
    for match in _QUALIFIED_ATTENDANCE_RE.finditer(flat):
        if _ENCLOSING_NON_MEMBER_LABEL_RE.search(flat[max(0, match.start() - 60) : match.start()]):
            continue  # "Guests: Commissioners in attendance: ..." -- the list is the guests'
        qualifier = match.group("qualifier")
        words = _qualifier_words(qualifier)
        if words & (_ROSTER_STAFF_WORDS | _ROSTER_EXCLUDED_WORDS):
            continue
        section = _roster_section(qualifier)
        if section != "members":
            continue
        names = _RUN_ON_LABEL_RE.sub("", match.group("names"))
        rows += _prose_roster_rows(names, "present", section, match.group(0))
    seen: set[tuple[str, str]] = set()
    unique = []
    for row in rows:
        key = (_roster_key(row["name"]), row["status"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def parse_roster(text: str) -> list[dict]:
    """Conservative member extraction from attendance lines; never invents names."""
    result: list[dict] = []
    for match in _ATTENDANCE_STATUS_RE.finditer(text):
        qualifier = match.group("qualifier")
        if _qualifier_words(qualifier) & _ROSTER_EXCLUDED_WORDS:
            continue
        status = match.group("status").lower()
        section = _roster_section(qualifier)
        names_text = match.group("names").strip()
        evidence_end = match.end()
        if names_text and section != "staff" and not _ends_a_sentence(names_text):
            # A long attendance line wraps ("... and Council Members Keely" / "Briggs, Jesse
            # Davis"): without the second line the member is kept as a bare first name.
            wrapped = _names_after_heading(text, match.end(), wrapped=True, previous=names_text)
            if wrapped is None:
                continue
            names_text = f"{names_text} {wrapped[0]}".strip()
            evidence_end = max(evidence_end, wrapped[1])
        if not names_text and section != "staff" and not _follows_heading(text, match.start()):
            # Staff blocks are "Name, Title; Name, Title" runs whose titles cannot be told from
            # names here, so only member headings read the lines below them.
            below = _names_after_heading(text, match.end())
            if below is None:
                continue
            names_text, evidence_end = below
        names_text = _PLURAL_ROSTER_TITLE_RE.sub(" ", _INLINE_LABEL_RE.sub(" ", names_text))
        for raw_name in _ATTENDANCE_SPLIT_RE.split(names_text):
            name = _clean_roster_name(raw_name)
            if not name:
                continue
            # Evidence is the heading plus any continuation lines the names came from.
            evidence = text[match.start() : evidence_end][:500]
            row = {"name": name, "status": status, "evidence": evidence}
            if section:
                row["section"] = section
            result.append(row)
    if not any(row.get("section") != "staff" for row in result):
        # Nothing structured named a member (a "STAFF PRESENT:" line alone does not count), so
        # read the prose; a structured member list stays authoritative when it exists.
        result = result + _parse_prose_attendance(text)
    if not result:
        return []
    # The attendance block is often repeated (a header on every page); keep each fact once.
    seen: set[tuple[str, str, str | None]] = set()
    unique = []
    for row in result:
        key = (_roster_key(row["name"]), row["status"], row.get("section"))
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


_VOTE_HEADING_RE = re.compile(r"\s*(?:item\s+)?(\d+[A-Za-z.]*)[.)\-:]\s*(.+)$", re.I)
_VOTE_KEYWORD_RE = re.compile(r"\b(vote|ayes?|nays?|motion|approved|opposed)\b", re.I)
_VOTE_MEMBER_FIRST_RE = re.compile(
    r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){1,3})\s*[-:–]\s*(yes|no|absent|recused|abstain(?:ed)?)\b",
    re.I,
)
_VOTE_VALUE_FIRST_RE = re.compile(
    r"\b(yes|no|absent|recused|abstain(?:ed)?)\s*[:=-]\s*([^;]+)", re.I
)
_VOTE_SPLIT_RE = re.compile(r",|\band\b")


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
        heading = _VOTE_HEADING_RE.match(line)
        if heading:
            current_item = heading.group(2).strip()
        if not _VOTE_KEYWORD_RE.search(line):
            continue
        for name, value in _VOTE_MEMBER_FIRST_RE.findall(line):
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
        for value, names in _VOTE_VALUE_FIRST_RE.findall(line):
            for name in _VOTE_SPLIT_RE.split(names):
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
