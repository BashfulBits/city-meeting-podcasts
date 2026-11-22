### Fixed

- Minutes extraction (#2782): Granicus `MinutesViewer.php` links whose Google Docs viewer wraps a PDF
  on a sibling `*.granicus.com` host (Pflugerville serves PDFs from `legistar1.granicus.com`) are now
  unwrapped instead of stored as the viewer shell (`Loading…`). Stored shells are no longer re-parsed
  from cache: `MINUTES_ROSTER_PARSER_VERSION` moves to 3 and `_stored_minutes_text` returns nothing for
  a shell, so affected episodes re-fetch the real document once. Unwrapping stays limited to the same
  host or two hosts under `granicus.com`.
