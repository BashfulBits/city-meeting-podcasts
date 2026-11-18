### Added

- Add a model-neutral torch sink for the authenticated streaming loader (`TorchSink`,
  `CollectSink`) with FP8 quantize-on-complete, and `load_karpo`, a streaming loader for Karpo
  decision models (Gemma 4 12B and 31B, Qwen3.5 4B and 9B), plus `load_hf_backbone` for the
  weights of Copernicus-31B and elephant-1. Revisions are pinned with their exact weight sizes.
  Weights go straight to the GPU through verified slices and are quantized one tensor at a time,
  so Karpo 12B fits an L4 without loading BF16 first and without a 24 GB host copy. On an L4 the
  12B loads in 103 s at 313 MB/s with 1.55 GiB of host RAM (8 MiB slices) and in 80 s at
  408 MB/s with 1.76 GiB (32 MiB slices), with a 12.8 GiB GPU peak (Phase 0: about 250 s and
  24 GB of host RAM). The 9B loads in 92 s (FP8) or 69 s (BF16), the 4B in 56 s, and on an RTX
  PRO 6000 the 31B loads in 236 s as FP8 (31 GiB) and 173 s as BF16 (58 GiB), each with under
  2 GiB of host RAM. In each tested configuration every streamed tensor matched, byte for byte,
  the same model loaded through its own path at the same precision (for FP8 that is Karpo's
  quantizer applied after a BF16 load; for 12B the Phase 0 layer-wise path); answer parity is
  reported separately. Adds a manifest derivation script, a cloud probe and calibration profiles.
- Add decision adapters for elephant-1 (OneJev `qev` and the checkpoint's own engine and readout)
  and Copernicus-31B (its own scoring backend and engine), in
  `citypods.judging.open_weight_runtimes`. They stream the weights through the loader, check the
  pinned author sources by SHA-256 before importing them, and answer with the Karpo adapter's call.
  Answers equal the authors' own loading path on 60 sample questions (RTX; on an L4 elephant-1
  keeps its embedding table and head in host memory, computes the head in float32 and chunks
  the MLPs, mean difference 0.001). elephant-1 does not fit an L4 as authored (22.4 GiB peak);
  with those options a 32,400-token state with 50 questions (39,795 tokens) ran at a 20.6 GiB
  peak. Adds the image recipe and parity job, and measured calibration rows for both models.
