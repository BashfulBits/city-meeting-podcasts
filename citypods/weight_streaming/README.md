# Authenticated safetensors streaming

This package is part of City Meeting Podcasts and is distributed under the
repository's MIT [license](../../LICENSE). It has no GPU or model dependency.
It delivers verified tensor segments to an adapter supplied by the model owner.

The trust root is the manifest: obtain its complete bucket, object name,
generation, size, chunk size and per-chunk SHA-256 values from a source whose
integrity and authenticity you independently verify. Never construct it from
untrusted object metadata alone. The hashes must cover the exact generation in
order, including the safetensors header. This package does not create, sign, or
authenticate manifests. The GCS composition below binds all of those fields to
one generation-pinned request stream. An authenticated access token is supplied
separately; it is never included in the manifest.

```python
from citypods.weight_streaming import GCSManifest, stream_gcs_safetensors

# `trusted` was independently authenticated before this point.
manifest = GCSManifest(
    bucket=trusted.bucket, name=trusted.name, generation=trusted.generation,
    size=trusted.size, chunk_size=trusted.chunk_size, sha256=tuple(trusted.hashes),
)

def copy_segment(spec, offset, data):
    # Validate spec.name, spec.shape and spec.dtype against the destination model;
    # synchronously copy data into that tensor starting at byte offset.
    adapter.copy(spec, offset, data)

try:
    stream_gcs_safetensors(manifest, access_token, copy_segment, workers=16)
    adapter.check_complete()  # Reject missing, duplicate and incomplete tensors.
except BaseException:
    adapter.discard()  # A failure may have left partial weights in GPU memory.
    raise
```

A sink must consume each view synchronously and must not retain it. It must check
that all required tensors occur exactly once and have the expected dtype, shape,
and full byte coverage; the parser verifies the file layout but cannot know a
model's expectations. Individual chunks are verified before delivery, but a later
failure can leave earlier verified bytes written. Never use a partially loaded
model. The file header must fit in the first chunk; unsupported or sub-byte
dtypes are rejected. At most 32 reads can be outstanding; each is at most 64 MiB,
plus the current verified chunk and the parsed header. Set the worker count and
chunk size to fit the host memory budget. The lower-level `stream_safetensors`
accepts a custom fetcher, whose identity and immutable version must be bound by
its caller to the authenticated manifest.
