### Added

- Minutes (#2782): a scanned minutes PDF with no text layer is now OCR'd with the agenda lane's
  existing Tesseract engine (`extract_minutes_document`). OCR runs only when the text layer is
  sparse and states no member roll call or votes, so the two sources are never mixed. It keeps
  Tesseract's line breaks so attendance headings parse, runs over at most 120 pages inside one
  deadline sized from the page count, and is discarded when it reports under 85% mean confidence.
  A missing or failed Tesseract is deferred for a later run instead of cached as an empty roster,
  and OCR is not started once the run budget is spent. Stored minutes record their page count and
  how they were obtained, so legacy extracts are re-fetched once, mostly blank scans are caught
  by density, and accepted OCR text is reused on parser upgrades. Denton has 433 scanned minutes,
  none of which had a roster. The minutes stage version moves to 3.
