- **Prioritize Kilo over OpenRouter for Nemotron routes.** Reordered
  `kilo_nvidia_nemotron_3_ultra_550b_a55b_free` and `kilo_nemotron_3_5_super_vl_free` ahead of their
  OpenRouter siblings in `config/provider_limits.yml` and recompiled route catalogs. Removed the
  artificial 100k TPM rate ceiling on Kilo routes to allow large meeting transcripts up to the
  observed input ceiling.
