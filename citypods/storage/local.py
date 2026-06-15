"""Local-filesystem storage backend for development and tests.

Writes audio under a root directory and serves it from a URL prefix. In a local
build this lands inside ``docs/`` so the generated feeds resolve against the same
``python -m http.server`` that serves the site. Not intended for production hosting
(use R2) — committing/serving large audio from Pages does not scale.
"""

from __future__ import annotations

import os
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
        return self.root / key

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

    # --- orphan GC support (optional StorageBackend capability) ---

    def list_objects(self, prefix: str = "") -> Iterator[tuple[str, datetime]]:
        if not self.root.exists():
            return
        for p in self.root.rglob("*"):
            if p.is_file():
                key = p.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    yield key, datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def put_text_if_absent(
        self, key: str, text: str, content_type: str = "text/plain; charset=utf-8"
    ) -> bool:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return True
