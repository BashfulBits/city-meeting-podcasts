"""Lane caps derived from meetings per day (review/58 section 12.11).

The seven production lanes' caps used to be hand-derived numbers in `config/site_config.yml`. They
are now computed from per-lane demand per meeting and one shared input, new meetings per day,
which scales with the active feeds in `config/feeds`. These tests pin three things:

1. The committed inputs reproduce the hand-derived caps of review/58 section 12.4 exactly.
2. More meetings per day raise the caps; fewer active feeds never lower them below the floor.
3. Growth that no longer fits the Worker's daily budgets fails the compile instead of clamping.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from citypods.compute.llm_lanes import (
    clear_cache,
    count_active_feeds,
    load_lanes,
    parse_lane_sizing,
    parse_lanes,
    sized_caps,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE = yaml.safe_load((REPO_ROOT / "config" / "site_config.yml").read_text(encoding="utf-8"))
BASIS = SITE["llm_lane_sizing"]["basis_active_feeds"]

# review/58 section 12.4, as merged in #2677: (reserved jobs, ceiling jobs, max_queued_jobs).
HAND_DERIVED = {
    "chapter-agenda": (10, 110, 220),
    "chapter-locator": (10, 110, 220),
    "topic-tags:tagger": (40, 650, 1300),
    "r6-moments": (10, 110, 220),
    "judge:anchor": (20, 120, 240),
    "judge:sibling": (30, 130, 260),
    "judge:adjudicator": (10, 110, 220),
}


def _lanes_at(active_feeds: int):
    sizing = parse_lane_sizing(SITE["llm_lane_sizing"], active_feeds=active_feeds)
    return sizing, parse_lanes(SITE["llm_lanes"], sizing=sizing)


def _reserved_jobs(lane) -> int:
    return lane.reserved_write_units // lane.ingress_write_units_per_job


def _write_feed(directory: Path, name: str, status: str | None = None) -> None:
    feed = {"slug": name}
    if status is not None:
        feed["lifecycle"] = {"status": status}
    (directory / f"{name}.yml").write_text(yaml.safe_dump(feed), encoding="utf-8")


class TestCommittedInputs:
    def test_basis_inputs_reproduce_the_hand_derived_caps(self):
        sizing, lanes = _lanes_at(BASIS)
        assert sizing.meetings_per_day == 14
        assert {p for p, lane in SITE["llm_lanes"].items() if "sizing" in lane} == set(HAND_DERIVED)
        for purpose, (reserved, ceiling, queue) in HAND_DERIVED.items():
            lane = lanes[purpose]
            units = lane.ingress_write_units_per_job
            assert lane.reserved_write_units == reserved * units, purpose
            assert lane.daily_write_units == ceiling * units, purpose
            assert lane.max_queued_jobs == queue, purpose

    def test_unsized_lane_keeps_its_stated_caps(self):
        _, lanes = _lanes_at(BASIS)
        remedy = lanes["remedy:p3"]
        assert (remedy.reserved_write_units, remedy.daily_write_units) == (0, 800)
        assert remedy.max_queued_jobs == 800

    def test_every_ceiling_at_once_matches_section_12_4(self):
        from scripts import compile_llm_lanes

        sizing, lanes = _lanes_at(BASIS)
        totals = compile_llm_lanes.check_ceilings_fit(
            lanes, sizing, unit_budget=38_400, job_budget=6_000, row_pace=85_000
        )
        assert totals["jobs"] == 1_540
        assert totals["units"] == 8_440
        assert totals["rows"] == pytest.approx(32_640)

    def test_fewer_active_feeds_never_lower_the_caps(self):
        _, at_basis = _lanes_at(BASIS)
        sizing, fewer = _lanes_at(BASIS // 2)
        assert sizing.meetings_per_day == 14
        assert fewer == at_basis


class TestScaling:
    def test_twice_the_feeds_roughly_doubles_the_reservations(self):
        sizing, doubled = _lanes_at(2 * BASIS)
        _, today = _lanes_at(BASIS)
        assert sizing.meetings_per_day == 28
        expected = {
            "chapter-agenda": 10,
            "chapter-locator": 10,
            "topic-tags:tagger": 70,
            "r6-moments": 20,
            "judge:anchor": 40,
            "judge:sibling": 50,
            "judge:adjudicator": 20,
        }
        assert {p: _reserved_jobs(doubled[p]) for p in expected} == expected
        # Rounding up to 10 jobs keeps the small lanes flat; the total still nearly doubles.
        total_today = sum(_reserved_jobs(today[p]) for p in expected)
        total_doubled = sum(expected.values())
        assert 1.6 <= total_doubled / total_today <= 2.0
        # The ceiling grows by exactly the reservation's growth: the backlog allowance is fixed.
        for purpose in expected:
            grew = doubled[purpose].daily_write_units - today[purpose].daily_write_units
            assert (
                grew == doubled[purpose].reserved_write_units - today[purpose].reserved_write_units
            )

    def test_unrounded_demand_is_linear_in_meetings(self):
        entry = {"jobs_per_meeting": 0.5, "headroom": 2, "backlog_jobs": 0, "drain_days": 1}
        raw = {
            **SITE["llm_lane_sizing"],
            "round_jobs_to": 1,
            "basis_active_feeds": 10,
            "meetings_per_day_p90": 10,
        }
        base = parse_lane_sizing(raw, active_feeds=10)
        doubled = replace(base, active_feeds=20)
        assert sized_caps(entry, base, units_per_job=4, purpose="x")[0] == 10 * 4
        assert sized_caps(entry, doubled, units_per_job=4, purpose="x")[0] == 20 * 4

    def test_growth_past_the_daily_budget_fails_the_compile(self):
        from scripts import compile_llm_lanes

        sizing, lanes = _lanes_at(40 * BASIS)  # ~560 meetings/day; the row pace binds near 410
        with pytest.raises(SystemExit, match="no longer fit the day"):
            compile_llm_lanes.check_ceilings_fit(
                lanes, sizing, unit_budget=38_400, job_budget=6_000, row_pace=85_000
            )

    def test_row_pace_alone_can_fail_the_compile(self):
        from scripts import compile_llm_lanes

        sizing, lanes = _lanes_at(BASIS)
        with pytest.raises(SystemExit, match="EARLY_SLOT_RELEASE_ROW_PACE"):
            compile_llm_lanes.check_ceilings_fit(
                lanes, sizing, unit_budget=38_400, job_budget=6_000, row_pace=30_000
            )


class TestValidation:
    def _site_lane(self, **overrides):
        entry = {
            "models": ["m1"],
            "telemetry": {
                "producer": "test",
                "unit": "episode",
                "completion": "consumed",
                "scope": "retained_catalog",
            },
            "sizing": {"jobs_per_meeting": 1, "headroom": 1, "backlog_jobs": 0, "drain_days": 1},
        }
        entry.update(overrides)
        return {"a-purpose": entry}

    def test_sized_lane_needs_the_shared_block(self):
        with pytest.raises(ValueError, match="llm_lane_sizing"):
            parse_lanes(self._site_lane())

    def test_sized_lane_must_not_also_state_caps(self):
        sizing = parse_lane_sizing(SITE["llm_lane_sizing"], active_feeds=BASIS)
        with pytest.raises(ValueError, match="computed"):
            parse_lanes(self._site_lane(daily_write_units=100), sizing=sizing)

    def test_sizing_entry_rejects_unknown_or_missing_fields(self):
        sizing = parse_lane_sizing(SITE["llm_lane_sizing"], active_feeds=BASIS)
        with pytest.raises(ValueError, match="must define exactly"):
            parse_lanes(self._site_lane(sizing={"jobs_per_meeting": 1}), sizing=sizing)

    def test_shared_block_rejects_a_missing_field(self):
        raw = dict(SITE["llm_lane_sizing"])
        del raw["meetings_per_day_p90"]
        with pytest.raises(ValueError, match="meetings_per_day_p90"):
            parse_lane_sizing(raw)

    def test_shared_block_rejects_a_bool(self):
        with pytest.raises(ValueError, match="must be a number"):
            parse_lane_sizing({**SITE["llm_lane_sizing"], "meetings_per_day_p90": True})


class TestFeedCount:
    def test_counts_only_active_feeds(self, tmp_path):
        _write_feed(tmp_path, "_template")
        _write_feed(tmp_path, "a")
        _write_feed(tmp_path, "b", "active")
        _write_feed(tmp_path, "c", "paused")
        _write_feed(tmp_path, "d", "dormant")
        _write_feed(tmp_path, "e", "retired")
        assert count_active_feeds(tmp_path) == 2

    def test_missing_directory_counts_zero(self, tmp_path):
        assert count_active_feeds(tmp_path / "nope") == 0

    def test_committed_feeds_are_counted(self):
        assert count_active_feeds(REPO_ROOT / "config" / "feeds") >= 1

    def test_load_lanes_scales_with_the_feeds_beside_the_site_config(self, tmp_path):
        config = tmp_path / "config"
        feeds = config / "feeds"
        feeds.mkdir(parents=True)
        site = {
            "llm_lane_sizing": {**SITE["llm_lane_sizing"], "basis_active_feeds": 2},
            "llm_lanes": {"r6-moments": SITE["llm_lanes"]["r6-moments"]},
        }
        (config / "site_config.yml").write_text(yaml.safe_dump(site), encoding="utf-8")
        for name in ("a", "b", "c", "d"):
            _write_feed(feeds, name)
        clear_cache()
        try:
            lane = load_lanes(path=config / "site_config.yml")["r6-moments"]
        finally:
            clear_cache()
        # 28 meetings/day: 0.213 x 28 x 3 = 17.9 -> 20 jobs reserved, ceiling 120.
        assert lane.reserved_write_units == 20 * lane.ingress_write_units_per_job
        assert lane.daily_write_units == 120 * lane.ingress_write_units_per_job
        assert lane.max_queued_jobs == 240
