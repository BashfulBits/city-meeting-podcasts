"""Cross-run build state and content-hash change detection.

``docs/`` is rebuilt fresh on every CI run, so two pieces of state that MUST survive
between runs live outside it, under a dedicated ``state_dir`` (default
``.citypods-state``) that CI restores via ``actions/cache``:

  * the audio manifest (per city) — without it the per-run materialize budget is wasted
    re-confirming already-hosted objects via ``storage.exists`` HEADs, so backfill never
    progresses as the catalog grows;
  * the change-detection cache — ETag/Last-Modified when a provider supplies them, plus a
    **content hash** for the providers that don't (Granicus/Swagit expose no usable HTTP
    validator), so an unchanged city skips re-materialization and re-rendering.

The content hash also folds in a **build fingerprint** (template bytes + base URL + a
manual version bump), so a template/domain/code change busts every city's cache and forces
a re-render even when the upstream meeting list is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from citypods.render import TEMPLATE_DIR
from citypods.statesync import pull_state
from citypods.storage import make_storage

DEFAULT_STATE_DIR = ".citypods-state"
ETAG_CACHE_NAME = "feed_etags.json"

# Bump when a code change should invalidate every cached render even though templates and
# upstream content are unchanged (e.g. feed-builder logic changes not visible in templates).
BUILD_VERSION = "1"


def resolve_state_dir(site_config: dict, output_dir: Path) -> Path:
    """Where persistent build state lives. Configurable via ``site_config.state_dir``;
    defaults to ``.citypods-state`` next to (not inside) the published ``output_dir``."""
    raw = site_config.get("state_dir") or DEFAULT_STATE_DIR
    path = Path(raw)
    if not path.is_absolute():
        path = Path(output_dir).resolve().parent / path
    return path


def pull_canonical_state(
    site_config: dict,
    output_dir: str | Path,
    *,
    base_url: str = "",
    log: Callable[[str], None] | None = None,
) -> Path:
    """Resolve ``state_dir`` and pull the durable snapshot from the bucket into it.

    Shares the "construct storage, then pull" idiom every read path needs (``run.py``'s
    builder and the feed-health audit) so a future change to that sequence (e.g. CAS-key
    skip logic) only needs editing once. The bucket is canonical
    (``citypods.statesync``'s documented contract) — ``actions/cache`` is a pure latency
    optimization elsewhere, never a correctness dependency, so a missing/unreachable bucket
    degrades to "whatever's already on disk" rather than failing the caller outright.
    """
    emit = log or (lambda message: print(message, flush=True))
    state_dir = resolve_state_dir(site_config, output_dir)
    try:
        storage = make_storage(site_config, base_url, output_dir)
        restored = pull_state(storage, state_dir, log=emit)
    except Exception as exc:  # noqa: BLE001 — state unavailable must not abort the caller
        emit(f"state: could not pull canonical state from the bucket ({exc}); using local copy")
        return state_dir
    if restored:
        emit(f"state: restored {restored} file(s) from durable storage")
    return state_dir


@lru_cache(maxsize=1)
def _template_fingerprint() -> str:
    h = hashlib.sha256()
    for tmpl in sorted(TEMPLATE_DIR.glob("*.j2")):
        h.update(tmpl.name.encode())
        h.update(tmpl.read_bytes())
    return h.hexdigest()


def build_fingerprint(base_url: str) -> str:
    """A short digest of everything that affects rendered output but isn't part of the
    upstream meeting list: template bytes, the deployment base URL, and BUILD_VERSION."""
    h = hashlib.sha256()
    h.update(BUILD_VERSION.encode())
    h.update(base_url.encode())
    h.update(_template_fingerprint().encode())
    return h.hexdigest()[:16]


def load_etag_cache(state_dir: Path) -> dict:
    path = state_dir / ETAG_CACHE_NAME
    return json.loads(path.read_text()) if path.exists() else {}


def save_etag_cache(state_dir: Path, cache: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / ETAG_CACHE_NAME
    path.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    from citypods.statesync import mark_state_dirty

    mark_state_dirty(state_dir, path.relative_to(state_dir))
