### Added

- Add a model-neutral torch sink for the authenticated streaming loader (`TorchSink`,
  `CollectSink`) with FP8 quantize-on-complete, and `load_karpo`, a streaming loader for Karpo
  decision models (Gemma 4 and Qwen3.5). Weights go straight to the GPU through verified slices
  and are quantized one tensor at a time, so Karpo 12B fits an L4 without loading BF16 first and
  without a 24 GB host copy. On an L4 the 12B loads in 80 to 103 s at 313 to 408 MB/s (Phase 0:
  about 250 s) with 1.6 GiB of host RAM and a 12.8 GiB GPU peak, and the 9B loads in 92 s with
  9.2 GiB of weights. All 1,006 (12B) and 960 (9B) tensors are byte-identical to Karpo's own
  layer-wise path. Adds a manifest derivation script, a cloud probe and calibration profiles
  `karpo-12b-fp8` and `karpo-9b-fp8`.
