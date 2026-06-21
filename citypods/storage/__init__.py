"""Storage backend selection.

``make_storage`` picks a backend from the ``AUDIO_STORAGE_BACKEND`` env var (handy for
local dev, e.g. ``AUDIO_STORAGE_BACKEND=local``) or ``site_config.defaults
.audio_storage_backend``:
  - ``"local"``: writes under ``<output_dir>/audio`` served at ``<base_url>/audio``
  - ``"r2"`` / ``"b2"``: S3-compatible object storage from env (None if creds absent)
  - ``"routing"``: B2 primary + R2 coordination behind ``RoutingStorage`` (review/17 §5).
    Coordination keys route to R2 for CAS; everything else stays on B2. Degrades to the
    B2 primary when R2 creds are absent.
"""

from __future__ import annotations

import os
from pathlib import Path

from citypods.storage.base import StorageBackend
from citypods.storage.local import LocalStorage
from citypods.storage.routing import COORDINATION_PREFIXES, RoutingStorage
from citypods.storage.s3 import CASConflict, S3CompatibleStorage, b2_from_env, r2_from_env

AUDIO_PREFIX = "audio"

_S3_PRESETS = {"r2": r2_from_env, "b2": b2_from_env}


def make_storage(site_config: dict, base_url: str, output_dir: Path) -> StorageBackend | None:
    """Return a configured backend, or None if hosting is unavailable.

    None means the caller should skip materialization (e.g. cloud creds not set in a
    dry run); providers that *require* hosting will then drop unhosted episodes.
    """
    defaults = site_config.get("defaults", {})
    backend = os.environ.get("AUDIO_STORAGE_BACKEND") or defaults.get(
        "audio_storage_backend", "local"
    )
    if backend == "local":
        return LocalStorage(
            root=Path(output_dir) / AUDIO_PREFIX,
            url_prefix=f"{base_url.rstrip('/')}/{AUDIO_PREFIX}",
        )
    if backend == "routing":
        primary = b2_from_env()
        if primary is None:
            return None
        return RoutingStorage(primary=primary, coordination=r2_from_env())
    if backend in _S3_PRESETS:
        return _S3_PRESETS[backend]()
    raise ValueError(f"unknown audio_storage_backend: {backend!r}")


__all__ = [
    "StorageBackend",
    "LocalStorage",
    "S3CompatibleStorage",
    "RoutingStorage",
    "CASConflict",
    "COORDINATION_PREFIXES",
    "make_storage",
    "AUDIO_PREFIX",
]
