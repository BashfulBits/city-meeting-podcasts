### Added

- Minutes (#2782): a scanned minutes PDF with no text layer is now OCR'd with the agenda lane's
  existing Tesseract engine (`extract_minutes_document`). OCR keeps Tesseract's line breaks so
  attendance headings parse, runs over at most 120 pages inside the per-page time budget, and is
  discarded when Tesseract is missing, times out or reports under 85% mean confidence. Denton
  has 433 such minutes, none of which had a roster. Name cleaning also strips hyphenated and
  OCR-spaced "Vice-Chair", "Chair Pro Tem" and "Members" labels. The minutes stage version moves
  to 3 so markers certifying an empty extraction re-fetch once.
