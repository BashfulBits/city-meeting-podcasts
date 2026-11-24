### Fixed
- The input-limit search now paces its probes against the account's per-minute token budget
  (`tpm`), sharing one rolling one-minute ledger per provider account across the run. Probes used
  to drain the budget themselves, so Gemini routes got 429s at every size and stayed unmeasured.
