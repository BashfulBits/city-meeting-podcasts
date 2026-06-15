"""Object-storage contract for hosting materialized audio files."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Stores audio objects under string keys and returns public URLs.

    Keys are slash-separated, e.g. ``"denton-tx/clip-123.m4a"``.
    """

    name: str

    def exists(self, key: str) -> bool: ...

    def put_file(self, key: str, local_path: Path, content_type: str) -> str:
        """Upload ``local_path`` to ``key``; return the public URL."""
        ...

    def public_url(self, key: str) -> str:
        """Public URL a podcast player can fetch ``key`` from."""
        ...

    # --- optional capabilities -------------------------------------------------------
    # ``get_file`` / ``list_objects`` / ``delete`` back durable state sync, lease cleanup, and
    # orphan GC. Not every backend implements them; callers must feature-detect via ``hasattr``.

    def get_file(self, key: str, local_path: Path) -> bool:
        """Download ``key`` into ``local_path``. Return False if the object is absent."""
        ...

    def list_objects(self, prefix: str = ""):
        """Yield ``(key, last_modified)`` for every object under ``prefix``."""
        ...

    def delete(self, key: str) -> None:
        """Delete the object at ``key``."""
        ...
