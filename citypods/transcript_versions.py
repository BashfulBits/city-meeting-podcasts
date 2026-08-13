"""Pipeline versions shared by transcript ingestion and work-queue classification."""

from __future__ import annotations

# WhisperX replaces stable-ts for computed provider alignment. The new recipe applies to every
# provider source format, so all prior provider-align artifacts are gradually regenerated.
# Version 5 corrected the worker recipe to use the configured WhisperX CTC model rather than the
# faster-whisper transcription model. Version 6 preserves coarse provider timing windows through
# the process-local backend and rejects unsafe multi-minute WhisperX sections before allocation.
PROVIDER_ALIGN_PIPELINE_VERSION = "6"
