Removed the baked-in GCP project identifiers: the evaluation image's `BASE_IMAGE` build argument and
the `CITYPODS_MODEL_BUCKET` variable (probe job and `download_glide_weights.py`) are now required,
with no project, number or registry defaults.
