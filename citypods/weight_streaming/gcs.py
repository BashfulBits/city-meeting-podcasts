"""Composition of a trusted object manifest and generation-pinned GCS transport."""

from dataclasses import dataclass

from .loader import SliceManifest, TensorSink, stream_safetensors
from .transport import gcs_range_fetcher


@dataclass(frozen=True)
class GCSManifest:
    """Object identity and slice hashes authenticated together by the caller."""

    bucket: str
    name: str
    generation: str
    size: int
    chunk_size: int
    sha256: tuple[str, ...]


def stream_gcs_safetensors(
    manifest: GCSManifest, token: str, sink: TensorSink, *, workers: int = 8
) -> None:
    """Stream only the object generation named by this trusted manifest."""
    fetch = gcs_range_fetcher(
        manifest.bucket, manifest.name, manifest.generation, manifest.size, token
    )
    slices = SliceManifest(manifest.size, manifest.chunk_size, manifest.sha256)
    stream_safetensors(slices, fetch, sink, workers=workers)
