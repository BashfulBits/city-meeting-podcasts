### Added

- **NVIDIA Nemotron 3.5 Super VL routes and Nemotron 3 aliases.** Configured direct NVIDIA and
  OpenRouter routes for `nvidia/nemotron-3.5-super-vl-120b-a12b` and
  `openrouter/nvidia/nemotron-3.5-super-vl:free`, pooling under the canonical 3.5 Super key with
  inherited limits. Aliased legacy Nemotron 3 Super selectors to the 3.5 pool, upgraded the
  `judge:sibling` lane slot to 3.5 Super VL, and updated the AI Gateway probe route.
