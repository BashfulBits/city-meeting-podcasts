"""Fail-closed retirement evidence; no provider calls or live publication here."""

from citypods.provider_catalog.apply import SOURCE_PATHS, EditPlan
from citypods.provider_catalog.evidence import digest
from citypods.provider_catalog.reconcile import _route_model_keys

BRANCH = "automation/provider-catalog-removals"
PR_MARKER = "<!-- citypods:provider-catalog-removals -->"
# A separate reviewed activation change follows deployed recovery-canary acceptance.
RETIREMENTS_ENABLED = False


def _retirement_proven(route, report, config):
    """Only fresh complete absence plus exact configured-account probes qualify.

    Report must come from fresh reconciliation, never directly from an edited issue marker.
    Route digests bind each probe to its account and the entire current route configuration.
    Every configured physical account serving this upstream must agree before removing one.
    """
    provider, model = route.get("provider"), route.get("upstream_model")
    catalog = (report.state.get("catalogs") or {}).get(provider) or {}
    if (
        catalog.get("provider") != provider
        or catalog.get("observed_at") != config.today.isoformat()
        or catalog.get("complete") is not True
        or not isinstance(catalog.get("models"), (list, tuple))
    ):
        return False
    records = catalog["models"]
    if any(not isinstance(record, (list, tuple)) or len(record) != 6 for record in records):
        return False
    if any(record[0] == model for record in records):
        return False
    accounts = {
        a.get("id")
        for a in (config.limits.get("providers", {}).get(provider, {}).get("accounts") or [])
    }
    serving = [
        r
        for r in config.limits.get("routes") or []
        if r.get("provider") == provider and r.get("upstream_model") == model
    ]
    if not serving or any(
        not r.get("account_id") or r["account_id"] not in accounts for r in serving
    ):
        return False
    for physical in serving:
        matches = [a for a in report.anomalies if a.route_id == physical.get("route_id")]
        if len(matches) != 1:
            return False
        proof = matches[0]
        if (
            proof.provider != provider
            or proof.model != model
            or proof.verdict not in {"retired", "not_served"}
            or proof.absent_from_catalog is not True
            or proof.observed_on != config.today.isoformat()
            or proof.config_digest != digest(physical)
            or proof.contended
        ):
            return False
    return True


def _repair_lanes(config, removed, retired=()):
    routes = list(config.limits.get("routes") or [])
    affected = set().union(*(_route_model_keys(r) for r in routes if r["route_id"] in removed))
    eligible = set().union(
        *(
            _route_model_keys(r)
            for r in routes
            if r["route_id"] not in removed
            and r["route_id"] not in retired
            and r.get("free") is True
            and r.get("rpd") != 0
        )
    )
    repairs, primaries, blocked = [], [], []
    for purpose, lane in sorted(config.lanes.items()):
        if not affected.intersection((*lane.models, *lane.backup_models)):
            continue
        keep = lambda model: model not in affected or model in eligible  # noqa: E731
        models = tuple(m for m in lane.models if keep(m))
        backups = tuple(m for m in lane.backup_models if keep(m))
        if lane.primary_model not in models:
            replacement = next((m for m in backups if m in eligible), None)
            if replacement is None:
                blocked.append(purpose)
                continue
            models = (replacement, *(m for m in models if m != replacement))
            # Repeating the promoted model preserves the existing backup retry allowance.
            # The lane contract permits this; remove only models that lost eligible routes.
            primaries.append(purpose)
        # A surviving primary/additional model may not rely on paid-only or paused capacity.
        if not set(models) & eligible:
            blocked.append(purpose)
            continue
        reasoning = tuple((m, level) for m, level in lane.reasoning if m in (*models, *backups))
        if (models, backups, reasoning) != (lane.models, lane.backup_models, lane.reasoning):
            repairs.append((purpose, models, backups, reasoning))
    return tuple(repairs), tuple(primaries), tuple(blocked)


def plan_retirements(report, config):
    """Prepare only independently proven safe removals, rebuilding against current main.

    This pure planner does not activate publication. New-job lane changes never rewrite queued
    job policies or completed artifacts; the separate Slice 3a path owns their recovery.
    """
    routes = list(config.limits.get("routes") or [])
    ids = [r.get("route_id") for r in routes]
    if not all(isinstance(rid, str) and rid for rid in ids) or len(set(ids)) != len(ids):
        raise ValueError("route IDs missing or ambiguous")
    removed, applied, deferred, rejected = [], [], [], []
    # A held-back dead route remains configured, but cannot prove a replacement is usable.
    retired = {r["route_id"] for r in routes if _retirement_proven(r, report, config)}
    for route in sorted(routes, key=lambda r: r["route_id"]):
        rid = route["route_id"]
        if not any(
            a.route_id == rid and a.verdict in {"retired", "not_served"} for a in report.anomalies
        ):
            continue
        if rid not in retired:
            deferred.append(f"{rid}: fresh complete absent catalog and all-account proof required")
            continue
        proposed = (*removed, rid)
        _, _, blocked = _repair_lanes(config, proposed, retired)
        if blocked or len(proposed) == len(routes):
            rejected.append(
                f"{rid}: no safe replacement; review pool alternatives for "
                + (", ".join(blocked) or "the route catalog")
            )
            continue
        removed.append(rid)
        applied.append(f"{rid}: remove definitively retired route")
    repairs, primaries, _ = _repair_lanes(config, removed, retired)
    return EditPlan(
        config.base_commit,
        tuple((p, digest(config.texts[p])) for p in SOURCE_PATHS),
        applied=tuple(applied),
        deferred=tuple(deferred),
        rejected=tuple(rejected),
        removed_routes=tuple(removed),
        lane_repairs=repairs,
        primary_changes=primaries,
        proposal_kind="removals",
    )
