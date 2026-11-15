- **Recover tagger work after failed dispatches.** Give each permitted terminal retry a stable new
  idempotency generation, and count a failed Worker job only once. Queue indexes now retain models
  whose context fits even when their static input ceiling does not; live admission remains the
  authority, and a bounded catalog reconciliation revisits existing queued indexes. Oversized
  chapter inputs split before deferral, and new terminal failures retain a safe failure class.
  Existing exhausted markers require a separately verified repair; no completed artifacts are
  invalidated. Tag batch sizing now treats Gemini TPM as input-only instead of charging the output
  reservation against that input limit. A 2026-11-15 Gemini 3.5 Flash Lite probe returned input-TPM
  429s at 400,011 and 230,011 provider-counted tokens, capacity 503s at 200,011 and 150,011, and
  success at 100,011. It did not
  establish an exact usable limit and leaves the configured ceiling unchanged.
