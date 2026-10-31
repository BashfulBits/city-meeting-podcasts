### Fixed
- **Modal budget estimates now price the whole bill.** `estimated_dollars_per_runtime_second` for
  Modal was the bare L4 GPU rate (`0.000222`), omitting CPU and memory (~11% of the Oct 2026
  cycle). Raised to an all-in `0.000265` (GPU + CPU/RAM + ~7.5% pad), so reservations and the
  runtime-fallback settlement run conservative. The $30 Modal credit hit its spend limit on
  Oct 30 and failed the Modal deploy.
