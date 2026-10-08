"""Fail-closed retirement evidence; no provider calls or live publication here."""

from citypods.provider_catalog.evidence import digest


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
