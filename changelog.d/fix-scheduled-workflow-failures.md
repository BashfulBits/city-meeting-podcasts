- **Repair scheduled evaluations and audits.** Deduplicate H15 samples across shared sources,
  score generated transcripts from local files, bound each sample to a ten-minute POSIX signal
  budget, and save completed evidence incrementally. Native inference can delay signal delivery;
  the workflow timeout remains the outer bound. No pipeline versions change or catalog backfill
  is triggered. Judge-audit sync now uses relative paths while preserving file ownership.
- **Expose safe retirement-control diagnostics.** Report dispatch operation, HTTP status or
  transport exception category without response bodies or credentials. Retry pause, resume, and
  pause-status transport failures once; quota reservation and admission remain single-attempt.
  Retirement still fails closed when exclusive dispatch control cannot be verified.
