- `scripts/derive_karpo_manifest.py` now validates the bucket name before building the request and
  refuses redirects, so its bearer token can only go to `storage.googleapis.com`.
