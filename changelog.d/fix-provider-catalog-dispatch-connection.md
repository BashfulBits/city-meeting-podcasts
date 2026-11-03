- **Disable dispatch keep-alive and pin catalog workflows to Ubuntu 24.04.**
  Send `Connection: close` in `DispatchPauseClient` requests to prevent urllib3 connection
  pool socket reuse across multi-minute probe spacing and canary intervals, eliminating
  transient connection resets on Worker reservations. Pin provider catalog reconciliation and
  command workflows to `ubuntu-24.04` to match runner policy and prevent runner-image drift.
