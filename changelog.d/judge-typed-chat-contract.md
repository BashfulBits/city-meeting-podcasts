- **Typed judge answers and live contract evaluation.** New chat packets require per-kind
  answer fields, including a choose letter; legacy replies remain readable. Add a direct frozen
  evaluation harness that records raw responses, schema/parser coverage and quote support by route.
  Chat request revision 3 changes newly planned sibling/adjudicator packet identities only.
  Stored judgments and pending pointers are left as-is; there is no automatic catalog backfill,
  JEV re-ask, question prompt-version bump or production-state cleanup.

- **Sibling quote policy.** Count parsed sibling decisions independently of quote quality,
  including stored rows;
  preserve quote failure metrics and the adjudicator evidence gate. No automatic re-asks.
