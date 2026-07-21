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

Required env: the R2 leg needs ``CLOUDFLARE_ACCOUNT_ID``, ``R2_BUCKET``, and the dedicated
``CLOUDFLARE_R2_LIFECYCLE_API_TOKEN`` (Workers R2 Storage Write). It uses Cloudflare's lifecycle
API rather than the S3-compatible endpoint, whose lifecycle calls are not authorized by the normal
R2 object credentials. The B2 leg needs ``b2_from_env`` vars (B2_ENDPOINT, B2_KEY_ID, B2_APP_KEY,
B2_BUCKET, B2_PUBLIC_BASE_URL). A wholly absent leg is skipped with a note, so this runs in partial
environments; a partially configured leg fails safely.

Usage:
    PYTHONPATH=. python scripts/apply_bucket_lifecycle.py [--apply] [--site-config PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote

import requests

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


def _r2_rest_rule(s3_rule: dict) -> dict:
    """Translate our narrow S3-shaped source rule to Cloudflare's R2 lifecycle API shape."""
    expiration = s3_rule.get("Expiration", {})
    days = expiration.get("Days")
    if not isinstance(days, int) or days < 1:
        raise ValueError(f"R2 scratch rule needs a positive Expiration.Days: {s3_rule!r}")
    return {
        "id": s3_rule["ID"],
        "enabled": s3_rule.get("Status") == "Enabled",
        "conditions": {"prefix": s3_rule["Filter"]["Prefix"]},
        "deleteObjectsTransition": {"condition": {"type": "Age", "maxAge": days * 86400}},
    }


def _managed_r2_rest(rules: list[dict]) -> list[dict]:
    return [r for r in rules if str(r.get("id", "")).startswith(reclaim.MANAGED_RULE_ID_PREFIX)]


def _merge_r2_rest_rules(existing: list[dict], managed: list[dict]) -> list[dict]:
    unmanaged = [
        r for r in existing if not str(r.get("id", "")).startswith(reclaim.MANAGED_RULE_ID_PREFIX)
    ]
    return unmanaged + list(managed)


class _R2LifecycleAPI:
    """Small, deliberately scoped client for Cloudflare R2 bucket lifecycle rules."""

    def __init__(self, *, account_id: str, bucket: str, token: str):
        self._url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{quote(account_id, safe='')}/r2/buckets/{quote(bucket, safe='')}/lifecycle"
        )
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _request(self, method: str, *, body: dict | None = None) -> dict:
        response = requests.request(method, self._url, headers=self._headers, json=body, timeout=30)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.ok or payload.get("success") is not True:
            errors = payload.get("errors") or []
            detail = (
                "; ".join(
                    f"{error.get('code', 'unknown')}: {error.get('message', 'unknown error')}"
                    for error in errors
                    if isinstance(error, dict)
                )
                or "no Cloudflare error detail returned"
            )
            raise RuntimeError(
                "Cloudflare R2 lifecycle API "
                f"{method} failed (HTTP {response.status_code}): {detail}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Cloudflare R2 lifecycle API {method} returned no result object")
        return result

    def get_rules(self) -> list[dict]:
        rules = self._request("GET").get("rules", [])
        if not isinstance(rules, list):
            raise RuntimeError("Cloudflare R2 lifecycle API returned a non-list rules value")
        return rules

    def put_rules(self, rules: list[dict]) -> None:
        self._request("PUT", body={"rules": rules})


def _reconcile_r2_lifecycle(api: _R2LifecycleAPI, desired: list[dict], *, apply: bool) -> bool:
    current = api.get_rules()
    print(f"\n[r2] current lifecycle rules ({len(current)}):")
    print(json.dumps(current, indent=2, default=str))

    final = _merge_r2_rest_rules(current, desired)
    if final == current:
        print("[r2] already up to date — no change.")
        return True

    print("\n[r2] desired managed rules:")
    print(json.dumps(desired, indent=2, default=str))
    if not apply:
        print("[r2] DRY-RUN — not writing (pass --apply to write).")
        return True

    api.put_rules(final)
    readback = _managed_r2_rest(api.get_rules())
    ok = all(rule in readback for rule in desired)
    if ok:
        print(f"[r2] applied and verified {len(desired)} managed rule(s).")
    else:
        print(
            "::error title=lifecycle::[r2] PUT did not take effect — read-back is missing managed "
            f"rules. Read-back managed rules: {json.dumps(readback, default=str)}"
        )
    return ok


def _r2_leg(*, apply: bool) -> bool:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    bucket = os.environ.get("R2_BUCKET")
    token = os.environ.get("CLOUDFLARE_R2_LIFECYCLE_API_TOKEN")
    if not any((account_id, bucket, token)):
        print("[r2] R2 env absent — skipping R2 scratch-expiration leg.")
        return True
    if not all((account_id, bucket, token)):
        print("::error title=lifecycle::[r2] incomplete R2 lifecycle API configuration.")
        return False
    source_rules = reclaim.build_r2_scratch_rules()
    # Data-loss guardrail: scratch-only, never live prefixes.
    reclaim.assert_r2_rules_scoped(source_rules)
    rules = [_r2_rest_rule(rule) for rule in source_rules]
    return _reconcile_r2_lifecycle(
        _R2LifecycleAPI(account_id=account_id, bucket=bucket, token=token), rules, apply=apply
    )


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
