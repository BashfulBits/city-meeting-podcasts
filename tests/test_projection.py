"""Golden tests for the resource projection model (pins review/03 numbers)."""

from __future__ import annotations

import math

from citypods.projection import (
    FeatureSpec,
    ModelInputs,
    at_scale,
    gb_per_episode,
    measured_inputs,
    project,
    savings_if_capped,
)


def test_episode_size():
    # 96 kbps mono AAC, 2h -> 86.4 MB
    assert gb_per_episode(96, 2) == 0.0864
    assert round(gb_per_episode(64, 1) * 1000, 1) == 28.8


def test_default_projection_golden():
    p = project(ModelInputs())  # 80 feeds, 50 eps, 2h, 96 kbps, host_frac 1, cap 25
    assert round(p.storage_gb, 1) == 345.6
    assert round(p.monthly_cost_usd, 2) == 2.01
    # cap of 25 binds; a full time-budget run could do ~160 → cap is the bottleneck
    assert p.time_bound_throughput == 160
    assert p.per_run_throughput == 25
    assert p.cap_is_bottleneck is True
    assert p.recommended_per_run == 160
    assert p.capacity_per_day == 100  # 25 × 4 runs/day
    assert p.full_backfill_episodes == 4000
    assert p.full_backfill_days == 40.0


def test_full_backfill_uses_full_artifact_tier_when_set():
    # Body-aware tiered retention (review/39): a from-scratch backfill materializes through the
    # full-artifact tier (2000/body), not just the feed-visible render cap (50/body) — unset
    # falls back to episodes_per_feed so pre-review/39 numbers are unchanged (see golden test).
    p = project(ModelInputs(full_artifact_episodes=2000))
    assert p.full_backfill_episodes == 80 * 2000
    assert p.full_backfill_episodes != project(ModelInputs()).full_backfill_episodes


def test_scale_to_1000_cities():
    p = at_scale(ModelInputs(), 1000)
    assert round(p.storage_gb / 1000, 2) == 4.32  # TB
    assert round(p.monthly_cost_usd, 0) == 26


def test_time_bounded_removes_bottleneck():
    p = project(ModelInputs(per_run_cap=None))  # time-bounded
    assert p.per_run_throughput == 160
    assert p.cap_is_bottleneck is False
    assert p.capacity_per_day == 640  # 160 × 4


def test_steady_state_keeps_up_at_1000():
    p = at_scale(ModelInputs(per_run_cap=None), 1000)
    # 1000 feeds × 1/wk ≈ 143/day inflow vs 640/day capacity
    assert round(p.inflow_per_day) == 143
    assert p.keeps_up is True


def test_feature_spec_backlog_and_storage():
    # "host all audio" on a feed set that's currently half-hosted: model the added storage + drain
    transcripts = FeatureSpec(
        name="transcripts", sec_per_ep=30, bytes_per_ep=200_000, affected_frac=1.0
    )
    p = project(ModelInputs(per_run_cap=None), features=[transcripts])
    f = p.features["transcripts"]
    assert f["backlog"] == 4000  # 80 × 50
    assert f["drain_days"] > 0 and math.isfinite(f["drain_days"])
    assert f["storage_gb_delta"] > 0


def test_measured_inputs_uses_observations():
    cities = [object()] * 120
    inp = measured_inputs(
        cities,
        durations=[3600, 7200, 10800],  # median 2h
        run_history=[{"materialize_seconds": 1800, "materialized": 20}],  # 90 s/ep
        hosted_feeds=60,
    )
    assert inp.feeds == 120
    assert inp.duration_hours == 2.0
    assert inp.sec_per_ep == 90.0
    assert inp.host_frac == 0.5


def test_archive_items_drives_storage_independently_of_render_cap():
    # With archive_items unset, storage uses the render cap (pre-#109 behavior, golden unchanged).
    assert round(project(ModelInputs()).storage_gb, 1) == 345.6
    # Retaining more than we render (append-only archive) scales storage by archive_items, not E.
    bigger = project(ModelInputs(archive_items=500))  # 10× the 50 rendered
    assert round(bigger.storage_gb, 1) == 3456.0


def test_savings_if_capped():
    inp = ModelInputs(archive_items=500)
    # A candidate at/above the retained count frees nothing.
    assert savings_if_capped(inp, 500)["monthly_cost_delta"] == 0
    assert savings_if_capped(inp, 1000)["monthly_cost_delta"] == 0
    # Below it, capping frees storage + B2 cost.
    s = savings_if_capped(inp, 50)
    assert s["candidate_items"] == 50
    assert s["storage_gb_freed"] > 0
    assert s["monthly_cost_delta"] > 0


def test_measured_inputs_seeds_archive_items():
    inp = measured_inputs([object()] * 10, archive_items=320)
    assert inp.archive_items == 320


def test_measured_inputs_graceful_without_data():
    inp = measured_inputs(
        [],
    )
    assert inp.feeds == ModelInputs().feeds  # falls back to defaults
