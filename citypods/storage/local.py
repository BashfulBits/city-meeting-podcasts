"""Local-filesystem storage backend for development and tests.

Writes audio under a root directory and serves it from a URL prefix. In a local
build this lands inside ``docs/`` so the generated feeds resolve against the same
``python -m http.server`` that serves the site. Not intended for production hosting
(use R2) — committing/serving large audio from Pages does not scale.
"""

from __future__ import annotations

import shutil
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
