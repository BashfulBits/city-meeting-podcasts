### Added

- Add a manual deferred decision pilot for frozen tag and moment questions. The offline rehearsal
  exercises the GLiDE worker and collection contract. Cloud launch starts disabled and supports
  a three-attempt canary comparing regular and delayed Cloud Run Jobs. The worker uses a separate
  pinned Linux GPU dependency lane with the GLiDE-9B CUDA 13.0 runtime.
- Add an experimental, opt-in authenticated streaming loader for GLiDE with a reusable
  model-neutral safetensors package, bounded range reads and trusted generation-pinned manifests.
  The existing local model path remains the default; one L4 canary passed structural validation,
  and an RTX PRO 6000 canary also passed structural validation; quality qualification remains
  pending.
- Add inference diagnostics to the decision-batch worker: a prompt-length distribution, per-forward
  timings, kernel-version and Triton-cache counters in the completion telemetry, an abort when
  the gated-delta-rule kernels fall back to PyTorch, and an opt-in warm-repeat probe.
