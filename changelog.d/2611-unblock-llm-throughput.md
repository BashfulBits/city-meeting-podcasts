- **Unblock LLM job submission and judging throughput.**
  - **Raise DO active bundle limit:** Increase `MAX_ACTIVE_BUNDLES` from 5 to 12 in dispatch tuning
    so fast worker bundles are not blocked by long-running Gemma/JEV claim bundles.
  - **Rule candidate judging slot load balancing:** Enable Slot B (`non_google`, e.g. Nemotron)
    alongside Slot A (`google`) to evaluate rule-candidate subjects in `sibling_candidates`, and
    raise `max_outstanding_packets.sibling` from 480 to 800 packets.
  - **Terminal failure recovery:** Add `scripts/clear_terminal_failures.py` CLI, storage helper
    `clear_terminal_failure()`, and `.github/workflows/clear-terminal-failures.yml` workflow to
    audit and prune poisoned failure markers for clean lineage retry.
