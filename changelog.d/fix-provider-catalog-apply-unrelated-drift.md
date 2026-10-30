- **Provider catalog `/apply` no longer defers on unrelated catalog changes.** Selections were
  compared against a digest of the provider's entire model listing, so any other model appearing or
  disappearing on a busy provider (Kilo, OpenRouter, NVIDIA, BeatAPI) turned every ticked candidate
  into "candidate changed". New evidence carries a digest of the model's own entries instead, and
  the whole-listing digest is no longer compared.
