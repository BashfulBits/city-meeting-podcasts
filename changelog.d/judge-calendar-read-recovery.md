### Fixed

- Defer a source calendar upload after exhausted transient storage reads instead of aborting
  judge finalization. Preserve both local and remote calendars and continue other sources;
  permission and programming errors still propagate. No artifact invalidation or backfill.
