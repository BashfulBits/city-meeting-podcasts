### Fixed
- Google AI Studio Gemma 31B routes no longer retry a saturated token minute exactly 60s after the
  last scheduled start. A new per-route `tpm_window_margin_seconds` (15 on `gemma_4_31b_primary`
  and `gemma_4_31b_secondary`) ages reservations out of the trailing-minute ledger, and out of the
  `own_tpm` cooldown, a little later, so arrival jitter cannot put two ~11k-token packets less than
  a minute apart at Google. Absent or 0 leaves every other route unchanged.
- The Worker now logs an `own_tpm_observed` event (reservation, ledger window tokens, entry ages)
  on each Google `own_tpm` 429, with no Durable Object rows, to size the margin from real data.
