"""Parametric resource cost/time projection (pure, dependency-free).

Answers the scoping questions the project actually asks:
  * What does B2 storage cost at N cities?
  * What if we host *all* audio (even where a direct video link exists)?
  * How many days of 6-hour Actions runs to drain the current/backfill backlog?
  * How does a new per-episode feature (e.g. transcripts) change storage + catch-up time?

All formulas are closed-form and unit-tested against golden numbers (see review/03-resource-model.md
for the derivations). Everything is driven by an explicit ``ModelInputs`` so the same model powers
the CLI report, the GitHub job summary, and the client-side what-if admin page.

B2 economics baked in: storage is the only material cost — egress is free via the Cloudflare
Bandwidth Alliance (or R2 native), and Class-A/B/C transactions are negligible at this volume.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Backblaze B2 pricing (2025): $6 / TB / month storage, first 10 GB free. Egress ~$0 (Cloudflare).
B2_USD_PER_GB_MONTH = 0.006
B2_FREE_GB = 10.0
GITHUB_JOB_LIMIT_HOURS = 6.0  # hard per-job ceiling on GitHub-hosted runners


def bytes_per_episode(kbps: int, hours: float) -> float:
    """Mono-AAC episode size in bytes: bitrate (bits/s) / 8 × seconds."""
    return kbps * 1000 / 8 * 3600 * hours


def gb_per_episode(kbps: int, hours: float) -> float:
    return bytes_per_episode(kbps, hours) / 1e9


@dataclass
class FeatureSpec:
    """A prospective per-episode feature's marginal cost, for what-if modeling.

    ``bytes_per_ep`` adds to storage; ``sec_per_ep`` adds to per-episode wall time; the one-time
    backlog it creates is ``feeds × E × affected_frac``.
    """

    name: str
    sec_per_ep: float = 0.0
    bytes_per_ep: float = 0.0
    affected_frac: float = 1.0


@dataclass
class ModelInputs:
    feeds: int = 80
    episodes_per_feed: int = 50  # E: how many a FEED renders/materializes (== max_episodes)
    # Append-only archive (issue #109) decouples what we STORE from what a feed renders: the
    # archive retains far more than max_episodes. ``archive_items`` is the storage driver (retained
    # records per feed). None -> fall back to episodes_per_feed (keeps pre-#109 numbers unchanged).
    archive_items: int | None = None
    duration_hours: float = 2.0  # D: average meeting length
    kbps: int = 96
    host_frac: float = 1.0  # fraction of feeds whose audio we host (HLS=1; direct only if extract)
    sec_per_ep: float = 90.0  # wall-seconds to materialize one episode (download+encode); MEASURE
    cycle_hours: float = 6.0  # hours between Actions runs
    time_budget_hours: float = 5.0  # usable wall-time per run (< 6h with headroom)
    safety: float = 0.8  # fraction of the time budget we actually fill
    per_run_cap: int | None = 25  # hard episode cap (materialize_budget_per_run); None = time-bound
    meetings_per_week: float = 1.0  # new meetings per feed per week (steady-state inflow)


@dataclass
class Projection:
    inputs: ModelInputs
    gb_per_ep: float
    storage_gb: float
    monthly_cost_usd: float
    per_run_throughput: int  # episodes materialized per run (the binding limit)
    time_bound_throughput: int  # episodes a full time-budget run could do (ignores cap)
    capacity_per_day: int
    inflow_per_day: float
    keeps_up: bool
    full_backfill_episodes: int
    full_backfill_days: float
    cap_is_bottleneck: bool
    recommended_per_run: int  # time-bounded budget the model suggests
    # name -> {backlog, drain_days, storage_gb_delta, monthly_cost_delta}
    features: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "inputs"}
        d["inputs"] = self.inputs.__dict__
        return d


def archived_per_feed(inputs: ModelInputs) -> int:
    """Records retained per feed = the storage driver. Falls back to the render cap when the
    archive size isn't known, so pre-#109 projections are unchanged."""
    return inputs.archive_items if inputs.archive_items is not None else inputs.episodes_per_feed


def _monthly_cost(storage_gb: float) -> float:
    return max(0.0, storage_gb - B2_FREE_GB) * B2_USD_PER_GB_MONTH


def savings_if_capped(inputs: ModelInputs, candidate_items: int) -> dict:
    """What ratcheting the retention cap down to ``candidate_items`` per feed would free. A
    candidate at/above the current retained count saves nothing (delta 0); below it frees the
    storage (and B2 $/mo) of the discarded older recordings. Powers the admin "cost of keeping
    old audio" readout (issue #109)."""
    g = gb_per_episode(inputs.kbps, inputs.duration_hours)
    hosted_feeds = inputs.feeds * inputs.host_frac
    current = archived_per_feed(inputs)
    candidate = min(candidate_items, current)
    cur_gb = hosted_feeds * current * g
    cand_gb = hosted_feeds * candidate * g
    return {
        "candidate_items": candidate,
        "storage_gb_freed": cur_gb - cand_gb,
        "monthly_cost_delta": _monthly_cost(cur_gb) - _monthly_cost(cand_gb),
    }


def project(inputs: ModelInputs, features: list[FeatureSpec] | None = None) -> Projection:
    g = gb_per_episode(inputs.kbps, inputs.duration_hours)
    hosted_feeds = inputs.feeds * inputs.host_frac
    storage_gb = hosted_feeds * archived_per_feed(inputs) * g
    monthly = _monthly_cost(storage_gb)

    time_bound = max(1, int(inputs.time_budget_hours * 3600 * inputs.safety / inputs.sec_per_ep))
    throughput = min(time_bound, inputs.per_run_cap) if inputs.per_run_cap else time_bound
    runs_per_day = 24 / inputs.cycle_hours
    capacity_day = int(throughput * runs_per_day)
    inflow_day = inputs.feeds * inputs.meetings_per_week / 7.0

    full_backfill = int(hosted_feeds * inputs.episodes_per_feed)
    backfill_days = full_backfill / capacity_day if capacity_day else float("inf")

    feat_out: dict = {}
    for f in features or []:
        backlog = int(inputs.feeds * inputs.episodes_per_feed * f.affected_frac)
        # a feature with its own per-episode time competes for the same window; model it solo
        f_window = inputs.time_budget_hours * 3600 * inputs.safety
        f_time_bound = max(1, int(f_window / max(f.sec_per_ep, 1e-9)))
        f_through = min(f_time_bound, inputs.per_run_cap) if inputs.per_run_cap else f_time_bound
        f_cap_day = int(f_through * runs_per_day)
        extra_gb = (
            inputs.feeds * inputs.episodes_per_feed * f.affected_frac * (f.bytes_per_ep / 1e9)
        )
        new_total = storage_gb + extra_gb
        feat_out[f.name] = {
            "backlog": backlog,
            "drain_days": (backlog / f_cap_day if f_cap_day else float("inf")),
            "storage_gb_delta": extra_gb,
            "monthly_cost_delta": (
                max(0.0, new_total - B2_FREE_GB) - max(0.0, storage_gb - B2_FREE_GB)
            )
            * B2_USD_PER_GB_MONTH,
        }

    return Projection(
        inputs=inputs,
        gb_per_ep=g,
        storage_gb=storage_gb,
        monthly_cost_usd=monthly,
        per_run_throughput=throughput,
        time_bound_throughput=time_bound,
        capacity_per_day=capacity_day,
        inflow_per_day=inflow_day,
        keeps_up=capacity_day >= inflow_day,
        full_backfill_episodes=full_backfill,
        full_backfill_days=backfill_days,
        cap_is_bottleneck=inputs.per_run_cap is not None and inputs.per_run_cap < time_bound,
        recommended_per_run=time_bound,
        features=feat_out,
    )


def at_scale(base: ModelInputs, feeds: int) -> Projection:
    """The projection if the catalog grew to ``feeds`` feeds (all other knobs unchanged)."""
    inp = ModelInputs(**{**base.__dict__, "feeds": feeds})
    return project(inp)


# ---- deriving real inputs from observed data (graceful when history is absent) ----------------


def measured_inputs(
    cities: list,
    *,
    durations: list[int] | None = None,
    run_history: list[dict] | None = None,
    hosted_feeds: int | None = None,
    archive_items: int | None = None,
    base: ModelInputs | None = None,
) -> ModelInputs:
    """Replace defaults with observed values where available.

    - ``feeds`` from the city list; ``host_frac`` from hosted/total when known.
    - ``duration_hours`` from the median episode duration when durations are supplied.
    - ``sec_per_ep`` from run history (total seconds / expensive encodes), preferring each run's
      ``materialize_encoded`` count and falling back to ``materialized`` only when a run predates
      that field, so cheap storage re-credits don't skew the cost-per-episode estimate.
    All optional — with no data this just returns ``base`` (defaults) with ``feeds`` filled in.
    """
    inp = ModelInputs(**(base.__dict__ if base else {}))
    if cities:
        inp.feeds = len(cities)
    if hosted_feeds is not None and inp.feeds:
        inp.host_frac = max(0.0, min(1.0, hosted_feeds / inp.feeds))
    if durations:
        s = sorted(d for d in durations if d and d > 0)
        if s:
            inp.duration_hours = round(s[len(s) // 2] / 3600, 2)
    if run_history:
        secs = sum(r.get("materialize_seconds", 0) for r in run_history)
        # Use expensive encodes only (not cheap storage re-credits) so sec/ep reflects real cost.
        eps = sum(
            r["materialize_encoded"]
            if r.get("materialize_encoded") is not None
            else r.get("materialized", 0)
            for r in run_history
        )
        if eps > 0 and secs > 0:
            inp.sec_per_ep = round(secs / eps, 1)
    if archive_items is not None:
        inp.archive_items = archive_items
    return inp
