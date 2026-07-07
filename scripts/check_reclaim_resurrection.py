#!/usr/bin/env python
"""Resurrection watchdog for reclaimed storage objects (GH#496).

The failure this catches: the orphan GC deleted key ``K`` believing it unreferenced, then a later
build re-referenced ``K`` (a GH#421-style identity/coalescing flip-flop) — so a *live record now
points at an object we deleted*. On B2 the deleted version is still restorable, but only until the
retention window closes. This watchdog runs every scheduled reclaim cycle and asks one question:

    referenced_audio_keys(live)  ∩  {reclaim-log keys whose recover_by is still in the future}

Any non-empty intersection is data-loss-in-progress. It is a *deterministic* check — no need to
intercept every read — because the only way a deleted object matters again is if a record references
it, and that shows up in the live set on the next state pull.

On a hit the script writes a report for the workflow to open/refresh ONE HIGH-priority issue
(``issue-body.md`` + ``resurrection.json`` + ``has_resurrection``), mirroring the audio-GC issue
pattern, and exits non-zero so the run is visibly red. On a clean sweep it writes
= ``false`` so the workflow can close any open issue. R2 deletes carry no ``recover_by`` (R2 has no
version history) and so are never flagged here — they were unrecoverable the moment they happened,
which is why the R2-is-ephemeral invariant (``routing.py``) forbids canonical data there.

Usage:
    PYTHONPATH=. python scripts/check_reclaim_resurrection.py [--pull-state] [--out DIR] [--no-fail]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from citypods.config import load_site_config
from citypods.ops import reclaim_log
from citypods.records import referenced_audio_keys
from citypods.state import resolve_state_dir
from citypods.storage import make_storage


def find_resurrections(
    state_dir: Path, *, now: datetime | None = None
) -> list[reclaim_log.DeletionEntry]:
    """Reclaim-log entries whose key is back in the live set while still restorable. Newest-deadline
    ordering is applied by the caller for reporting."""
    now = now or datetime.now(UTC)
    referenced = referenced_audio_keys(state_dir)
    recoverable = {e.key: e for e in reclaim_log.load_recoverable(state_dir, now=now)}
    return [recoverable[k] for k in referenced if k in recoverable]


def render_issue_body(hits: list[reclaim_log.DeletionEntry]) -> str:
    earliest = min((h.recover_by for h in hits if h.recover_by), default="unknown")
    lines = [
        f"## 🚨 {len(hits)} reclaimed object(s) re-referenced — restore before {earliest}",
        "",
        "A record now references an object the storage-reclaim GC previously deleted. On B2 the "
        "prior version is still restorable, but **only until each `recover_by` deadline below** — "
        "the version is purged and the data is unrecoverable. Restore the affected object's prior "
        "version from the B2 bucket (or re-materialize it) **before** its deadline.",
        "",
        "| Key | Backend | Deleted | Restore before | Reaped by |",
        "|---|---|---|---|---|",
    ]
    for h in sorted(hits, key=lambda e: e.recover_by or ""):
        run = f"[run]({h.run_url})" if h.run_url else "—"
        lines.append(f"| `{h.key}` | {h.backend} | {h.deleted_at} | **{h.recover_by}** | {run} |")
    lines += [
        "",
        "How to restore (B2, S3 API): list object versions for the key, find the version just "
        "before the delete marker, and copy it back to the current key. If the delete marker was "
        "purged, re-materialize the artifact by re-running its build for the owning source/uid.",
    ]
    return "\n".join(lines) + "\n"


def _write_report(out_dir: Path, hits: list[reclaim_log.DeletionEntry]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "count": len(hits),
        "hits": [
            {
                "key": h.key,
                "backend": h.backend,
                "deleted_at": h.deleted_at,
                "recover_by": h.recover_by,
                "reason": h.reason,
                "run_url": h.run_url,
            }
            for h in sorted(hits, key=lambda e: e.recover_by or "")
        ],
    }
    (out_dir / "resurrection.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out_dir / "has_resurrection").write_text("true" if hits else "false", encoding="utf-8")
    if hits:
        (out_dir / "issue-body.md").write_text(render_issue_body(hits), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pull-state", action="store_true", help="restore durable state first")
    ap.add_argument("--site-config", default="config/site_config.yml")
    ap.add_argument("--output-dir", default="docs")
    ap.add_argument("--out", default=None, help="write the watchdog report into this dir")
    ap.add_argument(
        "--no-fail",
        action="store_true",
        help="exit 0 even on a hit (default: exit 2 so the run is red)",
    )
    args = ap.parse_args(argv)

    site_config = load_site_config(args.site_config)
    storage = make_storage(site_config, "https://example.invalid", args.output_dir)
    state_dir = resolve_state_dir(site_config, args.output_dir)
    if args.pull_state:
        if storage is None:
            # A safety-critical watchdog must never scan a stale/empty local state_dir and report
            # a clean sweep when the durable bucket state was never restored. If --pull-state was
            # asked for but no backend is configured, fail loudly instead of silently proceeding.
            print(
                "::error title=Reclaim watchdog::--pull-state requested but no storage backend is "
                "configured; refusing to scan possibly-stale local state."
            )
            return 2
        from citypods.statesync import pull_state

        pull_state(storage, state_dir)

    hits = find_resurrections(state_dir)
    if args.out:
        _write_report(Path(args.out), hits)

    if not hits:
        print("no reclaimed object is currently re-referenced within its recovery window — clean.")
        return 0

    earliest = min((h.recover_by for h in hits if h.recover_by), default="unknown")
    print(
        f"::error title=Reclaimed object re-referenced::{len(hits)} deleted object(s) referenced "
        f"again and still restorable; earliest recover_by {earliest}. See the opened issue."
    )
    for h in sorted(hits, key=lambda e: e.recover_by or ""):
        print(f"  RESURRECTED {h.key} (restore before {h.recover_by})")
    return 0 if args.no_fail else 2


if __name__ == "__main__":
    sys.exit(main())
