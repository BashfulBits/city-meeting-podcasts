#!/usr/bin/env python
"""Survey durable agenda_text_artifact documents for raw, undecoded PDF bytes.

Root cause: ``citypods/agenda_text.py``'s ``_extract_pdf`` fell back, when ``pypdf`` was not
importable, to decoding the PDF's own raw bytes as UTF-8 and returning that as if it were
extracted text. Real PDF container syntax and garbled compressed-stream bytes decode into
plausible, keyword-bearing noise rather than raising, so that corruption slipped past
``assess_agenda_document``'s quality gate and was persisted as a genuine ``agenda_text_artifact``.
Both the extraction fallback and the quality-gate gap are fixed (the ``%PDF-`` guard in
``_is_placeholder_text``), but any artifact produced *before* that fix is still corrupted on disk
and needs separate remediation.

Read-only: this reads each unique ``agenda_text_artifact_key`` directly from the B2 origin (a
bounded ``Range`` GET of the first few bytes -- never the public ``audio.citymeetings.fyi`` domain,
which fronts B2 through a rate/quota-limited Cloudflare Worker unsuited to a bulk one-off survey
like this) and flags any that still start with the raw PDF file signature. Keys are deduplicated
across every episode/city first -- content-addressed storage means many episodes can point at the
identical object, and this must read each one once, not once per episode that references it. It
does not re-extract, OCR, or mutate any record -- see GH#1092's existing placeholder-detection/OCR
retry path for how flagged episodes should be remediated once found (their `agenda_text_quality`
should be reset the way ``scripts/reset_agenda_chapter_state.py`` resets other bad-agenda-state
records, so AgendaTextStage re-derives them with the fixed extractor).

Requires a populated local state mirror (``--pull-state`` refreshes it from the canonical
object-store snapshot, same as ``scripts/research/agenda_chapters/audit_chapters.py``) and B2
credentials (``B2_ENDPOINT``/``B2_KEY_ID``/``B2_APP_KEY``/``B2_BUCKET``) in the environment.

Under ``--json``, stdout carries only ``{"hits": [...], "failed_keys": [...]}`` -- all
progress/diagnostic lines go to stderr, so redirecting stdout to a file always yields valid JSON.
A non-empty ``failed_keys`` means the survey is incomplete (some artifacts could not be read, so
they are neither confirmed clean nor confirmed corrupt); exit code is 3 in that case regardless of
whether any hits were also found, distinct from 1 (hits found, survey complete) and 0 (clean,
complete).

Usage:
    PYTHONPATH=. python scripts/audit_raw_pdf_agenda_artifacts.py --pull-state
    PYTHONPATH=. python scripts/audit_raw_pdf_agenda_artifacts.py --state-dir /path/to/state --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from citypods.config import load_city_configs, load_site_config
from citypods.records import load_records, source_key
from citypods.state import pull_canonical_state, resolve_state_dir
from citypods.storage.s3 import S3CompatibleStorage, b2_from_env

# Bounded: the raw-bytes bug's signature always sits at byte 0 of the persisted artifact, so a
# small prefix is enough -- no need to download every artifact's full (up to 50k-char) body.
_PROBE_BYTES = 64
# A courtesy pace against B2's own Class-B (download transaction) allowance -- direct-to-origin
# reads are far cheaper than the CF-worker-proxied public URL, but this still issues one request
# per *unique* stored artifact, and a large catalog means thousands of them.
_REQUEST_DELAY_SECONDS = 0.05


@dataclass(frozen=True)
class RawPdfHit:
    key: str
    prefix: str
    episodes: list[dict]  # [{"slug": ..., "uid": ...}, ...] -- every episode sharing this key


def _looks_like_raw_pdf(prefix: bytes) -> bool:
    """Cheap, structural check mirroring ``citypods.agenda_text._RAW_PDF_HEADER_RE``: real
    extracted text never starts with the PDF file signature -- only raw, undecoded container
    bytes do."""
    return prefix.decode("utf-8", errors="ignore").lstrip().startswith("%PDF-")


def collect_artifact_keys(cities, state_dir: Path) -> dict[str, list[dict]]:
    """Map each unique ``agenda_text_artifact_key`` to every (slug, uid) episode referencing it.

    Reading records is one bounded local-disk JSON load per city (`load_records`), never per
    episode -- the expensive part this script must bound is the *storage* read count, done next
    in `find_raw_pdf_artifacts` against the deduplicated key set this returns.
    """
    by_key: dict[str, list[dict]] = {}
    for city in cities:
        records = load_records(state_dir, source_key(city))
        for uid, record in records.items():
            if not isinstance(record, Mapping):
                continue
            key = (record.get("links") or {}).get("agenda_text_artifact_key")
            if not key:
                continue
            by_key.setdefault(key, []).append({"slug": city.slug, "uid": str(uid)})
    return by_key


def _check_one_key(
    key: str, episodes: list[dict], storage: S3CompatibleStorage
) -> tuple[RawPdfHit | None, bool]:
    """Return ``(hit_or_none, failed)``. ``failed=True`` means the read itself did not complete --
    distinct from a clean read that simply found nothing, so a failure can never look like a
    clean "not corrupted" result to a caller."""
    try:
        prefix = storage.get_range(key, 0, _PROBE_BYTES - 1)
    except Exception as exc:  # noqa: BLE001 - one bad read must not stop the survey
        print(f"[skip] {key}: read failed ({exc})", file=sys.stderr, flush=True)
        return None, True
    if prefix and _looks_like_raw_pdf(prefix):
        hit = RawPdfHit(key=key, prefix=prefix.decode("utf-8", errors="replace"), episodes=episodes)
        return hit, False
    return None, False


def find_raw_pdf_artifacts(
    by_key: dict[str, list[dict]], storage: S3CompatibleStorage, *, concurrency: int = 1
) -> tuple[list[RawPdfHit], list[str]]:
    """Check every unique key, serially or with a small bounded thread pool.

    A shared boto3 client is safe to call concurrently for reads (botocore's connection pool
    handles it -- default pool size 10, so keep ``concurrency`` at or below that). Each key is
    still read exactly once regardless of pool size; concurrency only shortens wall time.

    Returns ``(hits, failed_keys)``. All progress/diagnostic output goes to stderr, regardless of
    output mode -- stdout is reserved for the final report (plain text or, under ``--json``, the
    JSON document alone), never interleaved with it.
    """
    hits: list[RawPdfHit] = []
    failed_keys: list[str] = []
    items = sorted(by_key.items())
    total = len(items)
    checked = 0

    def _record(key: str, hit: RawPdfHit | None, failed: bool) -> None:
        nonlocal checked
        if hit is not None:
            hits.append(hit)
            print(f"[hit] {hit.key} ({len(hit.episodes)} episode(s))", file=sys.stderr, flush=True)
        if failed:
            failed_keys.append(key)
        checked += 1
        if checked % 200 == 0 or checked == total:
            print(
                f"[progress] {checked}/{total} unique artifacts checked",
                file=sys.stderr,
                flush=True,
            )

    if concurrency <= 1:
        for key, episodes in items:
            hit, failed = _check_one_key(key, episodes, storage)
            _record(key, hit, failed)
            time.sleep(_REQUEST_DELAY_SECONDS)
        return hits, failed_keys

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(_check_one_key, key, episodes, storage): key for key, episodes in items
        }
        for future in as_completed(futures):
            key = futures[future]
            hit, failed = future.result()
            _record(key, hit, failed)
    return hits, failed_keys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument(
        "--pull-state",
        action="store_true",
        help="Refresh the local state mirror from the canonical object-store snapshot first.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "Parallel B2 Range reads (default 1/serial). Keep at or below botocore's default "
            "connection-pool size (10) -- this hits the B2 origin directly, not the rate-limited "
            "public CF-worker domain, but stay bounded and courteous regardless."
        ),
    )
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")

    site_config = load_site_config(args.site_config)
    cities = load_city_configs(args.config_dir, site_config.get("defaults", {}))
    if args.pull_state:
        state_dir = pull_canonical_state(site_config, Path(args.output_dir))
    else:
        state_dir = args.state_dir or resolve_state_dir(site_config, Path(args.output_dir))

    storage = b2_from_env()
    if storage is None:
        print(
            "B2 credentials not found in the environment "
            "(B2_ENDPOINT/B2_KEY_ID/B2_APP_KEY/B2_BUCKET) -- cannot read artifacts directly.",
            file=sys.stderr,
        )
        return 2

    by_key = collect_artifact_keys(cities, state_dir)
    print(
        f"{len(by_key)} unique agenda_text_artifact object(s) across "
        f"{sum(len(v) for v in by_key.values())} episode reference(s).",
        file=sys.stderr,
        flush=True,
    )
    hits, failed_keys = find_raw_pdf_artifacts(by_key, storage, concurrency=args.concurrency)

    if args.json:
        print(
            json.dumps(
                {
                    "hits": [asdict(hit) for hit in hits],
                    "failed_keys": failed_keys,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        if not hits:
            print("No raw-PDF-bytes agenda_text_artifact documents found.")
        for hit in hits:
            episode_list = ", ".join(f"{ep['slug']}/{ep['uid']}" for ep in hit.episodes)
            print(f"{hit.key}: {episode_list}\n    {hit.prefix!r}")
        print(f"\n{len(hits)} affected object(s) found.")
    if failed_keys:
        # A failed read is not "clean" -- distinct exit code so a caller can never mistake an
        # incomplete survey (some keys simply weren't checked) for a confirmed-clean one, even
        # when zero hits were found among whatever did get checked.
        print(
            f"error: {len(failed_keys)} artifact read(s) failed; this survey is incomplete "
            "and its manifest must not be treated as exhaustive -- rerun before relying on it",
            file=sys.stderr,
        )
        return 3
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
