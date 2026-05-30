"""Storage backend selection.

``make_storage`` picks a backend from ``site_config.defaults.audio_storage_backend``:
  - ``"local"``: writes under ``<output_dir>/audio`` served at ``<base_url>/audio``
  - ``"r2"``: Cloudflare R2 from env (returns None if creds are absent)
"""

from __future__ import annotations

from pathlib import Path

from citypods.storage.base import StorageBackend
from citypods.storage.local import LocalStorage
from citypods.storage.r2 import R2Storage

AUDIO_PREFIX = "audio"


def make_storage(site_config: dict, base_url: str, output_dir: Path) -> StorageBackend | None:
    """Return a configured backend, or None if hosting is unavailable.

    None means the caller should skip materialization (e.g. R2 creds not set in a
    dry run); providers that *require* hosting will then drop unhosted episodes.
    """
    backend = site_config.get("defaults", {}).get("audio_storage_backend", "local")
    if backend == "local":
        return LocalStorage(
            root=Path(output_dir) / AUDIO_PREFIX,
            url_prefix=f"{base_url.rstrip('/')}/{AUDIO_PREFIX}",
        )
    if backend == "r2":
        return R2Storage.from_env()
    raise ValueError(f"unknown audio_storage_backend: {backend!r}")


__all__ = ["StorageBackend", "LocalStorage", "R2Storage", "make_storage", "AUDIO_PREFIX"]
