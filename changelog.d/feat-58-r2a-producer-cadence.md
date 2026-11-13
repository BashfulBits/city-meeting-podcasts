- **Review/58 12.1 producer cadence.** The tag producer now runs every two hours (Worker trigger
  `tag` at :14 on even UTC hours, GitHub fallback `19 */2 * * *`, guard window 90 minutes), up from
  every four. Moments joins the Worker trigger table (`moments` at :23 on odd UTC hours,
  `repository_dispatch`, one `28 1-23/2 * * *` GitHub fallback replacing nine irregular crons, the
  same schedule guard with a 90-minute window); its concurrency group is unchanged. Merging deploys
  the Worker because the compiled trigger table changed.
- **Moments reports no per-pass slot cap.** The moments run summary no longer prints the stale
  `r6-shared-moments-and-judge` slot-cap row (moments sets no per-run cap; the Worker's admission is
  the budget). A row appears only when ingress preflight closes the lane (cap 0), now labelled
  `r6-moments`.
- **Gemini Decision 1 limits.** The `gemini_decision_1_*` rpm/tpm/rpd comment now records them as
  owner-confirmed on 2026-11-13 (values unchanged).
