### Fixed

- **Ignored and delisted candidates no longer sit as "WAITING" on the provider-catalog issue.**
  The daily run carried every candidate from the last full run, even ones the maintainer had since
  ignored, so they showed "the next daily run tests it" for a test the loop skips by design. They
  are now dropped when carried, left out of the measurement plan, and a planned model that its
  provider no longer lists says so.
