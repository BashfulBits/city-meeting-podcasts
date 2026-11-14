- **Review/58 §12.5: 780 s ceiling for the slow reasoning routes.** `MAX_RESPONSE_SECONDS` is 780
  with a 45 s finalization reserve and an 870 s lease. Only OrcaRouter GLM 5.3 Flash and the three
  Nemotron 3 Ultra legs use 780 s. Every other route now pins its response timeout (720 or lower),
  and the compile script refuses an unpinned route while the ceiling is above 720. The Worker's B2
  result write is bounded by `RESULT_WRITE_TIMEOUT_SECONDS` (30 s), which settles as a retryable
  `server_error`. Nemotron 3 Ultra's verified reasoning-off switch on NVIDIA is declared but no
  lane uses it (agenda recall fell in the test; follow-ups in #2689). Merging deploys the Worker.
