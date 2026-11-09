### Fixed
- Judge lane: the per-pass quote-check read budget was a fixed 400 transcripts, so on 2026-11-09 a
  pass deferred 15,390 collected answers and left them counting against the outstanding-packet
  room. It is now `judging.quote_check_reads` in `site_config.yml` (2000).
