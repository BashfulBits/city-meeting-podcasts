- **Review/48 route-only adds of brand-new models, and report facts.** A route-only `/apply`
  for a model that no provider catalog or LiteLLM describes (the NVIDIA-hosted Muse Glimmer,
  MiniMax M3.1 Flash and Mistral Large 4) used to defer for a missing output-context value; it now
  gets a provisional 4,096-token output cap beside the provisional 16k input cap, flagged in the
  PR for a later discovery run (lane placement still needs measured values). Model facts ignore
  placeholder release dates (NVIDIA reports 1993-04-26 for every model) and NVIDIA card boilerplate,
  and the issue header says which floor reference models Artificial Analysis returns no
  Intelligence Index for.
