"""Local-filesystem storage backend for development and tests.

Writes audio under a root directory and serves it from a URL prefix. In a local
build this lands inside ``docs/`` so the generated feeds resolve against the same
``python -m http.server`` that serves the site. Not intended for production hosting
(use R2) — committing/serving large audio from Pages does not scale.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path


class LocalStorage:
    name = "local"

    def __init__(self, root: Path, url_prefix: str):
        self.root = Path(root)
        self.url_prefix = url_prefix.rstrip("/")

    def _path(self, key: str) -> Path:
        # CR2-CP-54: an absolute key overrides root entirely in pathlib's `/` operator, and a
        # `..`-laden key can walk out of root even when relative — neither is rejected by a raw
        # join. Resolve and confirm containment before returning; this backend is documented
        # dev/test-only and current key sources are pipeline-derived, not raw external input, but
        # the Protocol-level guarantee should hold regardless.
        candidate = (self.root / key).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"key {key!r} escapes storage root")
        return candidate

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def put_file(self, key: str, local_path: Path, content_type: str) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)
        return self.public_url(key)

    def public_url(self, key: str) -> str:
        return f"{self.url_prefix}/{key}"

    def get_file(self, key: str, local_path: Path) -> bool:
        src = self._path(key)
        if not src.exists():
            return False
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, local_path)
        return True

    def get_range(self, key: str, start: int, end: int) -> bytes | None:
        # CR2-CP-51: a negative start seeks from EOF (Python file-object semantics) and
        # end < start yields a negative read() length, which reads to EOF instead of a bounded
        # window. S3CompatibleStorage's equivalent invalid-range case (HTTP 416) already returns
        # None, not raises — match that contract here instead of silently reading the wrong bytes.
        if start < 0 or end < start:
            return None
        src = self._path(key)
        if not src.exists():
            return None
        with open(src, "rb") as f:
            f.seek(start)
            return f.read(end - start + 1)

    # --- orphan GC support (optional StorageBackend capability) ---

    def list_objects(self, prefix: str = "") -> Iterator[tuple[str, datetime]]:
        if not self.root.exists():
            return
        for p in self.root.rglob("*"):
            if p.is_file():
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    yield key, datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)

    def iter_objects(self, prefix: str = "") -> Iterator[tuple[str, datetime, int]]:
        if not self.root.exists():
            return
        for p in self.root.rglob("*"):
            if p.is_file():
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    stat = p.stat()
                    yield key, datetime.fromtimestamp(stat.st_mtime, tz=UTC), stat.st_size

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
