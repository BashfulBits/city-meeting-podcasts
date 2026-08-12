"""Pipeline versions shared by transcript ingestion and work-queue classification."""

from __future__ import annotations

# Provider alignment now understands Swagit's standalone ``[HH:MM:SS]`` text anchors and
# converts them into served-time coarse windows before stable-ts alignment.
PROVIDER_ALIGN_PIPELINE_VERSION = "3"
