### Fixed
- Re-arming a dispatch pause now retries a stalled Worker three times, after 2, 5 and 10 s,
  instead of once and at once. A single `ReadTimeout` pair on a pause renewal aborted run
  45550037105 after 15 minutes and discarded its state and evidence. Resume and status keep their
  single immediate retry, and admission and reservation calls still never retry.
