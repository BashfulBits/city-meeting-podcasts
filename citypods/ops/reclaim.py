"""Shared constants + pure helpers for the unified storage-reclaim policy (GH#496).

Three moving parts share these definitions so the numbers can never drift apart:

  * ``scripts/apply_bucket_lifecycle.py`` — configures the R2 scratch-expiration and B2
    version-retention lifecycle rules (infrastructure backstop for the validator's best-effort
    cleanup, and the recoverable-delete window for real B2 data).
  * ``scripts/gc_audio.py`` — the double-confirmed orphan auto-apply tier writes deletions with a
    ``recover_by`` derived from the B2 retention window.
  * ``scripts/check_reclaim_resurrection.py`` — the watchdog only escalates keys whose
    ``recover_by`` is still in the future, i.e. still restorable from the B2 version window.

**Two independent time windows — do not conflate them:**

* *Orphan quarantine period* (:data:`ORPHAN_QUARANTINE_DAYS`, pre-delete gate): how long an object
  must be continuously observed orphaned, across ≥2 scheduled runs, before the auto-apply tier
  deletes it. Each run re-derives the live set and drops any watched key that reappears, so this is
  an active detection window, not passive waiting. Answers *"is it safe to delete yet?"*
* *B2 retention window* (:data:`B2_RETENTION_DAYS`, post-delete gate): how long B2 keeps a
  deleted/overwritten version so a mistaken delete can be restored. Answers *"how long do I have to
  undo a mistake?"* Should be ≥ 2–3× the weekly reclaim cadence so several watchdog runs can catch a
  resurrection before the version purges.
"""

from __future__ import annotations

# --- pre-delete gate: orphan quarantine (Component 4) ---
# Default 21 days ⇒ with a weekly cadence an orphan is observed across ≥3 runs before auto-delete.
ORPHAN_QUARANTINE_DAYS_DEFAULT = 21.0

# --- post-delete gate: B2 recoverable-delete window (Components 1–3) ---
# Default 30 days ⇒ ~4 weekly watchdog runs can catch a resurrected reference before B2 purges the
# prior version. Confirm the live B2 value before changing (the user believes it is currently 7d).
B2_RETENTION_DAYS_DEFAULT = 30

# --- R2 validator-scratch lifecycle (Component 1, the GH#496 core) ---
# Exact, narrowly-scoped prefixes the validator writes scratch under (see validate_control_plane.py
# and provider_leases lease domains). The lifecycle rule expires objects here so a killed runner's
# best-effort cleanup can't leak scratch forever. TTL is 1 day: normal runs delete in seconds,
# this only ever catches crash-orphans.
R2_SCRATCH_PREFIXES: tuple[str, ...] = (
    "work-leases/__validate__/",
    "provider-leases/validate-",
)
R2_SCRATCH_TTL_DAYS = 1

# Bare coordination prefixes a lifecycle rule must NEVER target directly — expiring these deletes
# live leases/slots mid-job. The guardrail below refuses any R2 rule not strictly inside a scratch
# prefix, so these can only ever be matched by an over-broad (rejected) rule.
_R2_LIVE_COORDINATION_PREFIXES: tuple[str, ...] = ("work-leases/", "provider-leases/")


def orphan_quarantine_days(defaults: dict | None = None) -> float:
    """Resolve the orphan quarantine window from ``defaults.orphan_quarantine_days`` (tunable)."""
    if not defaults:
        return ORPHAN_QUARANTINE_DAYS_DEFAULT
    return float(defaults.get("orphan_quarantine_days", ORPHAN_QUARANTINE_DAYS_DEFAULT))


def b2_retention_days(defaults: dict | None = None) -> int:
    """Resolve the B2 recoverable-delete window from ``defaults.b2_retention_days`` (tunable)."""
    if not defaults:
        return B2_RETENTION_DAYS_DEFAULT
    return int(defaults.get("b2_retention_days", B2_RETENTION_DAYS_DEFAULT))


def _rule_prefix(rule: dict) -> str:
    """The object-key prefix a lifecycle rule targets, tolerating both the modern ``Filter.Prefix``
    shape and the legacy top-level ``Prefix`` some S3-compatible endpoints still return."""
    if "Filter" in rule and isinstance(rule["Filter"], dict):
        return rule["Filter"].get("Prefix", "")
    return rule.get("Prefix", "")


def build_r2_scratch_rules(*, ttl_days: int = R2_SCRATCH_TTL_DAYS) -> list[dict]:
    """S3 lifecycle rules expiring validator scratch under each :data:`R2_SCRATCH_PREFIXES`."""
    rules = []
    for prefix in R2_SCRATCH_PREFIXES:
        rid = "reclaim-r2-scratch-" + prefix.strip("/").replace("/", "-").replace("_", "")
        rules.append(
            {
                "ID": rid,
                "Filter": {"Prefix": prefix},
                "Status": "Enabled",
                "Expiration": {"Days": ttl_days},
            }
        )
    return rules


def build_b2_retention_rules(*, retention_days: int = B2_RETENTION_DAYS_DEFAULT) -> list[dict]:
    """S3 lifecycle rule keeping deleted/overwritten versions ``retention_days`` before purge, and
    cleaning up expired delete markers. Bucket-wide (``Prefix: ""``) because every real object needs
    the recovery window. NOTE: B2's S3 endpoint may ignore this shape and require its native
    ``daysFromHidingToDeleting`` rule — the apply script reads the config back to verify."""
    return [
        {
            "ID": "reclaim-b2-version-retention",
            "Filter": {"Prefix": ""},
            "Status": "Enabled",
            "NoncurrentVersionExpiration": {"NoncurrentDays": int(retention_days)},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
        }
    ]


# Lifecycle rules this policy owns are tagged with this ID prefix so a whole-policy PUT can replace
# *our* rules idempotently while preserving any unmanaged rules already on the bucket.
MANAGED_RULE_ID_PREFIX = "reclaim-"


def merge_managed_rules(existing: list[dict], managed: list[dict]) -> list[dict]:
    """Whole-policy PUT is replace-all, so build the desired policy by keeping every unmanaged rule
    already on the bucket (ID not starting with :data:`MANAGED_RULE_ID_PREFIX`) and appending our
    managed rules. Deterministic ordering (unmanaged first, then managed as given) so an unchanged
    config diffs equal and the apply script no-ops."""
    unmanaged = [r for r in existing if not str(r.get("ID", "")).startswith(MANAGED_RULE_ID_PREFIX)]
    return unmanaged + list(managed)


def assert_r2_rules_scoped(rules: list[dict]) -> None:
    """Data-loss guardrail: every R2 lifecycle rule must target a prefix strictly inside a scratch
    namespace (:data:`R2_SCRATCH_PREFIXES`). A bare ``work-leases/`` / ``provider-leases/`` (empty)
    prefix would expire live coordination objects mid-job — refuse to write it. ``startswith`` a
    scratch prefix guarantees the rule can only match scratch keys, since each scratch prefix is
    longer than its bare coordination parent."""
    for rule in rules:
        prefix = _rule_prefix(rule)
        if not any(prefix.startswith(scratch) for scratch in R2_SCRATCH_PREFIXES):
            raise AssertionError(
                f"R2 lifecycle rule prefix {prefix!r} is not strictly inside a validator scratch "
                f"namespace {R2_SCRATCH_PREFIXES}. Expiring a broader prefix (e.g. one of "
                f"{_R2_LIVE_COORDINATION_PREFIXES} or bucket-wide) would delete live leases/slots. "
                "Refusing to write it."
            )
