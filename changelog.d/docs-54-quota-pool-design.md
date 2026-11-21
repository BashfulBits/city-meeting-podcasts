- **Unified quota authority design (#2249).** Add review/54 §8.15 (L2, design only): the direct
  Python CAS ledger and the v2 Durable Object ledger can spend the same provider allowance without
  seeing each other. It records the code inventory, a read-only mapping step (key fingerprints, ledger
  use, retired-bucket references, per-pool counters), a recommendation to make the Durable Object the
  single authority for shared pools via reserve/settle endpoints, a staged migration with seeding and
  rollback, and the tests that would prove the allowance cannot be exceeded. No code, quota or bucket
  changes.
