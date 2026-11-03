### Changed
- **Review/48 request-cap discovery: typed size limits and slow routes.** A provider's typed
  `context_length` refusal (NVIDIA `details.limit/actual`) now steers the next input probe to 95%
  of the stated limit instead of ending as `inconclusive`; it is a hint, so only an admitted,
  provider-counted request becomes a cap. Input probes that time out, or succeed slower than a
  600 s budget, are no longer retried at the same size or proposed as caps: the search shrinks
  until a size fits the Worker's 720 s ceiling. A transient 5xx is retried twice with backoff
  before an axis is called an outage.
- **Review/48 issue and `/apply` clarity.** The rolling issue opens with a **Next steps** table
  (READY / WAITING / YOUR CALL, tested input and output caps, what to do) and repeats the verdict
  beside each model's checkboxes; probe diagnostics move into collapsible details. Every `/apply`
  deferral now says what to do, and a failed `/apply` names the failing step and test IDs instead
  of "verification or publication command failed". After the output search has run with no
  measurement or published claim, `/apply` uses a flagged provisional 4,096-token output limit
  rather than deferring forever.

### Fixed
- **Provider reconciliation no longer aborts on one failed dispatch reservation.** The reserve call
  retries a transient Worker `ConnectionError` and the health-recheck loop skips only that route
  (no request is sent without a reservation), instead of failing the whole 90-minute run and
  discarding its state.
