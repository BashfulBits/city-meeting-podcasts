#!/usr/bin/env python
"""Configure storage-reclaim lifecycle rules on R2 and B2 as-code (GH#496).

Two backstops, one idempotent entry point:

  * **R2 validator scratch** — expire objects under the validator's scratch prefixes
    (``work-leases/__validate__/`` and ``provider-leases/validate-``) after 1 day, so a killed run
    that never reaches ``validate_control_plane.py``'s best-effort cleanup can't leak scratch.
    This is the infrastructure fix CR-SC-15 asked for.
  * **B2 version retention** — keep deleted/overwritten object *versions* for a bounded window
    (``defaults.b2_retention_days``, default 30) before purge, and clean up expired delete markers.
    This is the recoverable-delete window the resurrection watchdog relies on: a mistakenly-reaped
    audio/transcript object can be restored from its prior version until the window closes.

Safe by construction:
  * reads the CURRENT lifecycle policy on each bucket and prints it, so you see the live state (the
    user believes B2 is at 7d) BEFORE anything changes;
  * a hard guardrail (``reclaim.assert_r2_rules_scoped``) refuses any R2 rule whose prefix is
    broader than a scratch namespace — an over-broad ``work-leases/`` rule would expire live leases;
  * ``--dry-run`` (default) only prints the diff; ``--apply`` writes, then READS BACK to verify the
    managed rules are present (catches an endpoint that silently ignores the PUT — notably B2, whose
    S3 lifecycle support may need its native rule shape instead).

Required env: the R2 leg needs ``r2_from_env`` vars (CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_BUCKET; R2_PUBLIC_BASE_URL optional for coordination-only). The B2 leg
needs ``b2_from_env`` vars (B2_ENDPOINT, B2_KEY_ID, B2_APP_KEY, B2_BUCKET, B2_PUBLIC_BASE_URL). A
env is absent is skipped with a note, so this runs in partial environments.

Usage:
    PYTHONPATH=. python scripts/apply_bucket_lifecycle.py [--apply] [--site-config PATH]
"""

from __future__ import annotations

import argparse
import json
import sys

from citypods.config import load_site_config
from citypods.ops import reclaim


def _managed(rules: list[dict]) -> list[dict]:
    return [r for r in rules if str(r.get("ID", "")).startswith(reclaim.MANAGED_RULE_ID_PREFIX)]


def _reconcile(storage, desired: list[dict], *, label: str, apply: bool) -> bool:
    """Diff the desired managed rules against the bucket's live policy, printing both. Writes only
    when ``apply`` and something changed, then reads back to verify. Returns True on success."""
    current = storage.get_lifecycle_rules()
    print(f"\n[{label}] current lifecycle rules ({len(current)}):")
    print(json.dumps(current, indent=2, default=str))

    final = reclaim.merge_managed_rules(current, desired)
    if final == current:
        print(f"[{label}] already up to date — no change.")
        return True

    print(f"\n[{label}] desired managed rules:")
    print(json.dumps(desired, indent=2, default=str))
    if not apply:
        print(f"[{label}] DRY-RUN — not writing (pass --apply to write).")
        return True

    storage.put_lifecycle_rules(final)
    readback = _managed(storage.get_lifecycle_rules())
    ok = all(rule in readback for rule in desired)
    if ok:
        print(f"[{label}] applied and verified {len(desired)} managed rule(s).")
    else:
        print(
            f"::error title=lifecycle::[{label}] PUT did not take effect — read-back is missing "
            f"managed rules. Endpoint may need a native (non-S3) rule shape. Read-back managed "
            f"rules: {json.dumps(readback, default=str)}"
        )
    return ok


def _r2_leg(*, apply: bool) -> bool:
    from citypods.storage.s3 import r2_from_env

    storage = r2_from_env(require_public_base_url=False)
    if storage is None:
        print("[r2] R2 env absent — skipping R2 scratch-expiration leg.")
        return True
    rules = reclaim.build_r2_scratch_rules()
    reclaim.assert_r2_rules_scoped(rules)  # data-loss guardrail: scratch-only, never live prefixes
    return _reconcile(storage, rules, label="r2", apply=apply)


def _b2_leg(*, apply: bool, retention_days: int) -> bool:
    from citypods.storage.s3 import b2_from_env

    storage = b2_from_env()
    if storage is None:
        print("[b2] B2 env absent — skipping B2 version-retention leg.")
        return True
    rules = reclaim.build_b2_retention_rules(retention_days=retention_days)
    return _reconcile(storage, rules, label="b2", apply=apply)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the rules (default: dry-run diff)")
    ap.add_argument("--site-config", default="config/site_config.yml")
    args = ap.parse_args(argv)

    defaults = load_site_config(args.site_config).get("defaults", {})
    retention_days = reclaim.b2_retention_days(defaults)
    print(
        f"storage-reclaim lifecycle — R2 scratch TTL {reclaim.R2_SCRATCH_TTL_DAYS}d, "
        f"B2 version retention {retention_days}d (apply={args.apply})"
    )

    r2_ok = _r2_leg(apply=args.apply)
    b2_ok = _b2_leg(apply=args.apply, retention_days=retention_days)
    return 0 if (r2_ok and b2_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
