### Added

- Add a model-neutral torch sink for the authenticated streaming loader (`TorchSink`,
  `CollectSink`) with FP8 quantize-on-complete, and `load_karpo`, a streaming loader for Karpo
  decision models (Gemma 4 12B and Qwen3.5 4B and 9B). Weights go straight to the GPU through
  verified slices and are quantized one tensor at a time, so Karpo 12B fits an L4 without loading
  BF16 first and without a 24 GB host copy. On an L4 the 12B loads in 103 s at 313 MB/s with
  1.55 GiB of host RAM (8 MiB slices) and in 80 s at 408 MB/s with 1.76 GiB (32 MiB slices), with
  a 12.8 GiB GPU peak (Phase 0: about 250 s and 24 GB of host RAM). The 9B loads in 92 s with
  1.49 GiB of host RAM and 9.2 GiB of weights. All 1,006 (12B) and 960 (9B) tensors are
  byte-identical to Karpo's own layer-wise path. Adds a manifest derivation script, a cloud probe
  and calibration profiles `karpo-12b-fp8` and `karpo-9b-fp8`.
