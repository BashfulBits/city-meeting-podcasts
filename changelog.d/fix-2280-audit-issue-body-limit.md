### Fixed

- Feed-health audit (#2280): a consolidated issue whose hidden state block would push the body past
  GitHub's 65,536-character limit now sheds state detail (long examples, then rows) instead of
  failing `createIssue` and turning the whole audit run red. This surfaced once the audit began
  reading canonical state and found a large `agenda-quality` finding.
