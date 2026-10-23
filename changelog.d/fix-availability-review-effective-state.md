- **H16 availability review: operator-overridden candidates now ingest.** The weekly digest
  publishes a candidate's *effective* verdict (`operator_override or state`), but `ingest` compared
  the issue marker against the raw `state` field. Every child whose recording had already been
  confirmed in an earlier review was therefore rejected as a stale candidate: the decision was not
  stored, the child never closed, and the batch stayed open with no visible error (#2315). Ingest
  now validates against the same effective verdict the digest rendered, while still rejecting a
  changed source fingerprint, detector version, or verdict.
