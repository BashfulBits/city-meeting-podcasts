"""One-off manual audit of the Stage-2 work-lease ledger (GH#706 §6(b) close-out).

``citypods compute reconcile`` never lists the raw ``work-leases/`` prefix (by design — review/18
§4.6 lever 1, to keep the sweep at ~1 Class-A op per completed transcript): it only revisits leases
for episodes that are still *candidates* — ``transcript-asr`` items freshly computed from the
current records as not-yet-``done`` (see ``report_workers.py``/``dispatch.reconcile_compute``).

While chasing down whether a specific stray/orphaned lease (from an accidental Modal/Beam
cancellation) was ever actually reaped, every scheduled ``compute reconcile`` run — before and
after the lease's ~20h TTL should have elapsed — reported ``0 in-flight`` for work-leases, which
would only happen if the candidate-derived sweep never saw it as ``leased`` at all. That's either
reassuring (it really is gone) or a sign the candidate list is missing an episode that should be
in it. This script lists the ledger directly, bypassing the candidate filter, to tell the two
apart. Not meant to run on a schedule — a manual, occasional diagnostic only.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from citypods.config import load_site_config
from citypods.ops.work_leases import LEASE_PREFIX, WorkLease
from citypods.ops.workqueue import load_manifest
from citypods.statesync import pull_state
from citypods.storage import make_storage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument("--base-url")
    args = parser.parse_args()

    site_config = load_site_config(args.site_config)
    output_dir = Path(args.output_dir)
    base_url = args.base_url or site_config.get("base_url", "")
    storage = make_storage(site_config, base_url, output_dir)
    if storage is None or not hasattr(storage, "list_objects"):
        print(json.dumps({"error": "storage unavailable or lacks list_objects"}))
        return 1

    now = datetime.now(UTC)
    now_str = now.isoformat()

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        snapshot = Path(td)
        pull_state(storage, snapshot)
        manifest_by_key = {(wi.source_key, wi.episode_uid): wi for wi in load_manifest(snapshot)}

    rows = []
    for key, _last_modified in storage.list_objects(f"{LEASE_PREFIX}/"):
        got = storage.get_bytes(key)
        if got is None:
            continue
        data, _etag = got
        try:
            lease = WorkLease.from_dict(json.loads(data))
        except (ValueError, TypeError, json.JSONDecodeError):
            rows.append({"key": key, "error": "unparseable"})
            continue
        if lease.state != "leased":
            continue
        expired = lease.is_expired(now)
        wi = manifest_by_key.get((lease.source_key, lease.uid))
        rows.append(
            {
                "key": key,
                "source_key": lease.source_key,
                "uid": lease.uid,
                "owner": lease.owner,
                "lease_expiry": lease.lease_expiry.isoformat() if lease.lease_expiry else None,
                "expired": expired,
                "manifest_state": wi.state if wi else "MISSING_FROM_MANIFEST",
                "manifest_work_class": wi.work_class if wi else None,
            }
        )

    print(json.dumps({"now": now_str, "leased_objects": rows, "count": len(rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
