### Fixed
- **CI: one hardened FFmpeg-library install for every workflow.** The apt step that survived a
  runner unable to resolve Ubuntu's mirrors (PR #2652, ci.yml only) is now
  `scripts/ci/install_ffmpeg_libs.sh`, called from ci.yml and the eleven other workflows that
  installed the libraries with an unbounded inline `apt-get` (audio, asr, asr-bench,
  asr-quality-eval, contracts, dep-bump-smoke, duration-normalize, granicus-probe,
  r7-diarization, remedy-p3-daily, remedy-unexpected-bodies). Each call site has a step timeout.
- **CI: the constraints drift gate fails fast instead of hanging.** On 2026-11-12 the `asr` lane's
  `pip-compile` went silent for 9+ minutes on four PRs (the image pull, DNS and the `prod` lane were
  fine). `scripts/compile_constraints.sh` now bounds pip's network waits, kills a lane after
  240 s, retries a timeout once, prints the verbose resolver log on failure, pulls the image with a
  timeout, and gives the container public DNS only when the host cannot resolve pypi.org. A
  timeout or resolution failure always fails the gate; real drift still fails it as before.
