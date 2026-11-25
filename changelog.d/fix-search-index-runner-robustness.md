### Fixed

- **Search index OOM & runner-loss recovery.** Externalized per-source partial caches from the
  monolithic `.search-cache.json` into `.search-partials/<source>.json` (with automatic migration
  of legacy checkpoints), bounded the in-memory sidecar `artifact_cache` to 64 MiB with per-source
  cleanup, added incremental working checkpoints per completed source, handled `SIGTERM` for
  graceful deferral, and added a 150-minute step timeout to `Build bounded search` in
  `.github/workflows/search.yml` so `Save resumable search checkpoint` always runs.
