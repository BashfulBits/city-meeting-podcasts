- **Review/48 report rework.** The rolling issue no longer carries the bulk reconcile memory
  (provider catalog snapshots, canary memory, structured-output evidence; 340 KB of JSON that
  filled GitHub's body limit and pushed the report out): it is written to a
  `provider-catalog-state-<run id>` artifact and the issue keeps only decision state plus the run
  ID and SHA-256; a missing or mismatching artifact only costs the next run its memory. The
  Artificial Analysis fetch now uses its documented endpoint (`/api/v2/data/llms/models`, no
  pagination), keeps the paginated one as a fallback, reports why
  at the top of the issue when every model is unscored, and matches `minimaxai` and MoE size tags
  (`-1t-a52b`). Candidates get short model-card facts (catalog description, size, release date,
  free-tier expiry, Hugging Face parameters and license, NVIDIA card summary). Decisions are laid
  out one section per model with its providers' rows underneath, every anomaly says what happens
  next, and a `structured_output_invalid` route with a verified alternative gets a tick that
  `/apply` re-verifies and applies. A route-only add with no published context now gets the
  provisional 16k cap.
