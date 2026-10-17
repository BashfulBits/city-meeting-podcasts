### Added

- **NVIDIA Nemotron 3.5 Super VL routes and Nemotron 3 aliases.** Configured direct NVIDIA and
  OpenRouter routes for `nvidia/nemotron-3.5-super-vl-120b-a12b` and
  `openrouter/nvidia/nemotron-3.5-super-vl:free`, pooling under the canonical 3.5 Super key with
  inherited rate and token limits. Configured cross-model routing and `also_serves` on the 3.5
  routes so that requests pinned to legacy Nemotron 3 Super routes can be satisfied by the
  upgraded 3.5 Super VL routes.
