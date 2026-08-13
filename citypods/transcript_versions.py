"""Pipeline versions shared by transcript ingestion and work-queue classification."""

from __future__ import annotations

# WhisperX replaces stable-ts for computed provider alignment. The new recipe applies to every
# provider source format, so all prior provider-align artifacts are gradually regenerated.
PROVIDER_ALIGN_PIPELINE_VERSION = "4"
