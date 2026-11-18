"""Authenticated, bounded-memory safetensors streaming primitives.

The caller must authenticate the manifest itself and discard any partially populated
model on failure. Sinks must synchronously consume each view and not retain it.
"""

from .gcs import GCSManifest, stream_gcs_safetensors
from .loader import SliceManifest, TensorSink, TensorSpec, stream_safetensors
from .transport import gcs_range_fetcher

__all__ = [
    "GCSManifest",
    "stream_gcs_safetensors",
    "SliceManifest",
    "TensorSpec",
    "TensorSink",
    "gcs_range_fetcher",
    "stream_safetensors",
]
