"""Explicit CDN reads for immutable public objects; regular storage reads stay direct."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from citypods.storage.base import StorageReadUnavailable


def supports_public_cdn(storage):
    primary = getattr(storage, "primary", storage)
    return getattr(primary, "name", None) == "b2" and callable(getattr(storage, "public_url", None))


def _read_origin_bytes(storage, key):
    if not key or not storage.exists(key) or not hasattr(storage, "get_file"):
        return None
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "object"
        return path.read_bytes() if storage.get_file(key, path) else None


def read_immutable_public_bytes(
    storage, key, session, metrics, *, prefer_cdn=True, validate=None, metric_prefix="transcript"
):
    """Read an immutable public object through the CDN, falling back to its owning backend.

    Callers must establish that the key is immutable and publicly served. Optional validation
    can reject stale or corrupt content. The session must enforce the project's HTTP URL policy.
    ``metric_prefix`` selects the names used for read telemetry.
    """
    from requests import RequestException

    use_cdn = prefer_cdn and session is not None and supports_public_cdn(storage)
    if use_cdn:
        metrics[f"{metric_prefix}_cdn_attempts"] += 1
        started = time.perf_counter()
        response = None
        try:
            response = session.get(storage.public_url(key), timeout=30)
            response.raise_for_status()
            data = bytes(response.content)
            metrics[f"{metric_prefix}_cdn_bytes"] += len(data)
            cache_status = str(response.headers.get("CF-Cache-Status", "")).upper()
            if cache_status == "HIT":
                metrics[f"{metric_prefix}_cdn_cache_hits"] += 1
            elif cache_status == "MISS":
                metrics[f"{metric_prefix}_cdn_cache_misses"] += 1
            else:
                metrics[f"{metric_prefix}_cdn_cache_status_other"] += 1
            if not data:
                metrics[f"{metric_prefix}_cdn_empty_responses"] += 1
            elif validate is not None and not validate(data):
                metrics[f"{metric_prefix}_cdn_integrity_mismatches"] += 1
            else:
                return data
        except RequestException:
            metrics[f"{metric_prefix}_cdn_request_failures"] += 1
        finally:
            if response is not None:
                response.close()
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            metrics[f"{metric_prefix}_cdn_elapsed_ms"] += elapsed_ms
            metrics[f"{metric_prefix}_cdn_elapsed_ms_max"] = max(
                metrics[f"{metric_prefix}_cdn_elapsed_ms_max"], elapsed_ms
            )
        metrics[f"{metric_prefix}_cdn_origin_fallbacks"] += 1
    elif not prefer_cdn:
        metrics[f"{metric_prefix}_cdn_evidence_digest_fallbacks"] += 1
    else:
        metrics[f"{metric_prefix}_storage_direct_reads"] += 1

    try:
        data = _read_origin_bytes(storage, key)
    except StorageReadUnavailable:
        if supports_public_cdn(storage):
            metrics[f"{metric_prefix}_b2_fallback_failures"] += 1
        raise
    if supports_public_cdn(storage):
        metrics[f"{metric_prefix}_b2_fallback_reads"] += 1
        metrics[f"{metric_prefix}_b2_fallback_bytes"] += len(data or b"")
    else:
        metrics[f"{metric_prefix}_storage_direct_bytes"] += len(data or b"")
    return data
