"""Lane reservations split from the daily row cap by ratio (review/58 section 12.11).

Each lane in `config/site_config.yml` states only a `reservation_ratio`. The reservations together
cost the Worker's daily row cap (`DO_ROWS_ENQUEUE_STOP` in `config/dispatch_tuning.yml`), split in
those ratios, so adding or removing a lane re-splits the budget with no other edit. These tests
pin the split, its invariants, and the compile-time guards.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from citypods.compute.llm_lanes import (
    clear_cache,
    jobs_per_ratio_point,
    live_reservation_rows,
    load_lanes,
    parse_lane_budget,
    parse_lanes,
    read_row_cap,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE = yaml.safe_load((REPO_ROOT / "config" / "site_config.yml").read_text(encoding="utf-8"))
TUNING = yaml.safe_load((REPO_ROOT / "config" / "dispatch_tuning.yml").read_text(encoding="utf-8"))
ROW_CAP = TUNING["DO_ROWS_ENQUEUE_STOP"]

# The ratios are the reservation jobs tested in #2677 (review/58 section 12.4).
RATIOS = {
    "chapter-agenda": 10,
    "chapter-locator": 10,
    "topic-tags:tagger": 40,
    "r6-moments": 10,
    "judge:anchor": 20,
    "judge:sibling": 30,
    "judge:adjudicator": 10,
    # Live lanes split the live pool by ratio: 100 and 50 of its 150 jobs (#2249).
    "interactive-classify": 0.5,
    "remedy:p3": 1,
}
LIVE = {"remedy:p3", "interactive-classify"}
# At DO_ROWS_ENQUEUE_STOP 90,000: the live pool (100 remedy jobs x 18 rows plus 50 interactive
# jobs x 24 rows) comes off the top, then k = 32.95 jobs per ratio point for the other lanes.
RESERVED_JOBS = {
    "chapter-agenda": 329,
    "chapter-locator": 329,
    "topic-tags:tagger": 1317,
    "r6-moments": 329,
    "judge:anchor": 658,
    "judge:sibling": 988,
    "judge:adjudicator": 329,
    "interactive-classify": 50,
    "remedy:p3": 100,
}


def _budget(row_cap: int = ROW_CAP):
    return parse_lane_budget(SITE["llm_lane_budget"], row_cap=row_cap)


def _lanes(block=None, row_cap: int = ROW_CAP):
    return parse_lanes(block or SITE["llm_lanes"], budget=_budget(row_cap))


def _jobs(lane) -> int:
    return lane.reserved_write_units // lane.ingress_write_units_per_job


def _rows(lanes, budget) -> float:
    return budget.fixed_rows_per_day + sum(
        _jobs(lane) * budget.rows_per_job(lane.ingress_write_units_per_job)
        for lane in lanes.values()
    )


class TestCommittedSplit:
    def test_ratios_are_the_reservations_tested_in_2677(self):
        assert {p: lane["reservation_ratio"] for p, lane in SITE["llm_lanes"].items()} == RATIOS

    def test_row_cap_is_the_workers_enqueue_stop(self):
        assert ROW_CAP == 90_000

    def test_reservations_split_the_row_cap(self):
        lanes = _lanes()
        assert jobs_per_ratio_point(lanes, _budget()) == pytest.approx(32.948, abs=0.001)
        assert {p: _jobs(lane) for p, lane in lanes.items()} == RESERVED_JOBS
        assert sum(RESERVED_JOBS.values()) == 4_429
        assert sum(lane.reserved_write_units for lane in lanes.values()) == 23_512
        rows = _rows(lanes, _budget())
        assert ROW_CAP - 100 < rows <= ROW_CAP

    def test_every_lane_may_borrow_up_to_the_reserved_total(self):
        lanes = _lanes()
        total = sum(lane.reserved_write_units for lane in lanes.values())
        assert {lane.daily_write_units for lane in lanes.values()} == {total}

    def test_queue_holds_two_days_of_the_reservation(self):
        lanes = _lanes()
        for purpose, lane in lanes.items():
            assert lane.max_queued_jobs == 2 * RESERVED_JOBS[purpose], purpose

    def test_live_lanes_are_the_flagged_ones(self):
        assert {p for p, lane in _lanes().items() if lane.live} == LIVE

    def test_live_lane_gets_the_whole_pool_and_can_borrow(self):
        remedy = _lanes()["remedy:p3"]
        assert _jobs(remedy) == 100
        assert SITE["llm_lane_budget"]["live_reservation_jobs"] == 150
        assert remedy.daily_write_units == 23_512
        assert remedy.max_queued_jobs == 200

    def test_load_lanes_matches_the_split(self):
        loaded = load_lanes()
        for purpose, lane in _lanes().items():
            assert loaded[purpose].reserved_write_units == lane.reserved_write_units


class TestResplit:
    def test_adding_a_lane_resplits_every_reservation(self):
        block = copy.deepcopy(SITE["llm_lanes"])
        block["new-lane"] = copy.deepcopy(block["judge:anchor"])
        lanes = _lanes(block)
        assert _jobs(lanes["new-lane"]) == _jobs(lanes["judge:anchor"]) > 0
        for purpose, jobs in RESERVED_JOBS.items():
            assert _jobs(lanes[purpose]) <= jobs, purpose
        assert _jobs(lanes["remedy:p3"]) == 100
        assert _jobs(lanes["topic-tags:tagger"]) < RESERVED_JOBS["topic-tags:tagger"]
        assert _rows(lanes, _budget()) <= ROW_CAP

    def test_removing_a_lane_resplits_every_reservation(self):
        block = copy.deepcopy(SITE["llm_lanes"])
        del block["judge:sibling"]
        lanes = _lanes(block)
        for purpose in block:
            assert _jobs(lanes[purpose]) >= RESERVED_JOBS[purpose], purpose
        assert _jobs(lanes["topic-tags:tagger"]) > RESERVED_JOBS["topic-tags:tagger"]
        assert _rows(lanes, _budget()) <= ROW_CAP

    def test_ratios_hold_between_lanes(self):
        lanes = _lanes()
        assert _jobs(lanes["topic-tags:tagger"]) == pytest.approx(
            4 * _jobs(lanes["chapter-agenda"]), abs=4
        )
        assert _jobs(lanes["judge:sibling"]) == pytest.approx(
            3 * _jobs(lanes["judge:adjudicator"]), abs=3
        )

    @pytest.mark.parametrize("row_cap", [45_000, 60_000, 90_000])
    def test_changing_the_row_cap_rescales_everything(self, row_cap):
        lanes = _lanes(row_cap=row_cap)
        budget = _budget(row_cap)
        scale = (row_cap - 1500 - 3000) / (ROW_CAP - 1500 - 3000)
        for purpose, jobs in RESERVED_JOBS.items():
            if purpose in LIVE:
                assert _jobs(lanes[purpose]) == jobs, purpose
            else:
                assert _jobs(lanes[purpose]) == pytest.approx(jobs * scale, abs=1), purpose
        assert _rows(lanes, budget) <= row_cap


class TestLivePool:
    def test_a_second_live_lane_splits_the_pool_by_ratio(self):
        block = copy.deepcopy(SITE["llm_lanes"])
        block["live-two"] = dict(copy.deepcopy(block["remedy:p3"]), reservation_ratio=3)
        lanes = _lanes(block)
        # 150 live jobs split 1 : 0.5 : 3.
        assert _jobs(lanes["remedy:p3"]) == 33
        assert _jobs(lanes["interactive-classify"]) == 16
        assert _jobs(lanes["live-two"]) == 100
        assert lanes["live-two"].max_queued_jobs == 200
        # Same pool of jobs: the other lanes move only by the rounding of the live lanes' rows.
        for purpose, jobs in RESERVED_JOBS.items():
            if purpose not in LIVE:
                assert _jobs(lanes[purpose]) == pytest.approx(jobs, abs=5), purpose

    def test_the_live_pool_comes_off_the_top(self):
        budget = _budget()
        lanes = _lanes()
        live_rows = sum(
            _jobs(lane) * budget.rows_per_job(lane.ingress_write_units_per_job)
            for lane in lanes.values()
            if lane.live
        )
        assert live_reservation_rows(lanes, budget) == live_rows
        weight = sum(
            RATIOS[p] * budget.rows_per_job(lane.ingress_write_units_per_job)
            for p, lane in lanes.items()
            if p not in LIVE
        )
        expected = (ROW_CAP - budget.fixed_rows_per_day - live_rows) / weight
        assert jobs_per_ratio_point(lanes, budget) == pytest.approx(expected)
        assert _rows(lanes, budget) <= ROW_CAP

    def test_no_live_lanes_split_the_whole_cap_as_before(self):
        block = copy.deepcopy(SITE["llm_lanes"])
        del block["remedy:p3"]
        del block["interactive-classify"]
        lanes = _lanes(block)
        budget = _budget()
        assert live_reservation_rows(lanes, budget) == 0
        # k = 88,500 / 2,595 rows per ratio point with no pool taken off.
        assert jobs_per_ratio_point(lanes, budget) == pytest.approx(34.104, abs=0.001)
        assert _jobs(lanes["topic-tags:tagger"]) == 1364

    def test_live_lanes_with_only_zero_ratios_fail(self):
        block = copy.deepcopy(SITE["llm_lanes"])
        block["remedy:p3"]["reservation_ratio"] = 0
        block["interactive-classify"]["reservation_ratio"] = 0
        with pytest.raises(ValueError, match="live_reservation_jobs pool has nothing to split"):
            _lanes(block)

    def test_live_needs_a_ratio(self):
        block = copy.deepcopy(SITE["llm_lanes"])
        del block["remedy:p3"]["reservation_ratio"]
        with pytest.raises(ValueError, match="sets live but no reservation_ratio"):
            _lanes(block)


class TestCompileGuards:
    def test_committed_split_fits_the_workers_guards(self):
        from scripts import compile_llm_lanes

        totals = compile_llm_lanes.check_reservations_fit(
            _lanes(),
            _budget(),
            unit_budget=TUNING["MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY"],
            job_budget=TUNING["MAX_JOBS_PER_UTC_DAY"],
        )
        assert totals["jobs"] == 4_429
        assert totals["units"] == 23_512

    def test_units_past_the_ingress_guard_fail_the_compile(self):
        from scripts import compile_llm_lanes

        with pytest.raises(SystemExit, match="MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY"):
            compile_llm_lanes.check_reservations_fit(
                _lanes(), _budget(), unit_budget=20_000, job_budget=6_000
            )

    def test_jobs_past_the_daily_job_guard_fail_the_compile(self):
        from scripts import compile_llm_lanes

        with pytest.raises(SystemExit, match="MAX_JOBS_PER_UTC_DAY"):
            compile_llm_lanes.check_reservations_fit(
                _lanes(), _budget(), unit_budget=38_400, job_budget=4_000
            )

    def test_row_cap_must_be_declared(self, tmp_path):
        tuning = tmp_path / "dispatch_tuning.yml"
        tuning.write_text("MAX_JOBS_PER_UTC_DAY: 6000\n", encoding="utf-8")
        with pytest.raises(ValueError, match="DO_ROWS_ENQUEUE_STOP"):
            read_row_cap(tuning)


class TestValidation:
    def _entry(self, **overrides):
        entry = {
            "models": ["m1"],
            "telemetry": {
                "producer": "test",
                "unit": "episode",
                "completion": "consumed",
                "scope": "retained_catalog",
            },
            "reservation_ratio": 1,
        }
        entry.update(overrides)
        return entry

    def test_ratio_needs_the_budget_block(self):
        with pytest.raises(ValueError, match="llm_lane_budget"):
            parse_lanes({"a": self._entry()})

    def test_ratio_lane_must_not_state_caps(self):
        with pytest.raises(ValueError, match="computed"):
            _lanes({"a": self._entry(daily_write_units=100)})

    def test_every_lane_or_none_states_a_ratio(self):
        stated = self._entry(daily_write_units=100)
        del stated["reservation_ratio"]
        with pytest.raises(ValueError, match="every lane states a reservation_ratio"):
            _lanes({"a": self._entry(), "b": stated})

    def test_all_zero_ratios_have_nothing_to_split(self):
        with pytest.raises(ValueError, match="non-live reservation_ratio is 0"):
            _lanes({"a": self._entry(reservation_ratio=0)})

    def test_negative_ratio_is_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            _lanes({"a": self._entry(reservation_ratio=-1)})

    def test_budget_rejects_a_missing_field(self):
        raw = dict(SITE["llm_lane_budget"])
        del raw["queue_days"]
        with pytest.raises(ValueError, match="queue_days"):
            parse_lane_budget(raw, row_cap=ROW_CAP)

    def test_row_cap_must_exceed_the_fixed_rows(self):
        with pytest.raises(ValueError, match="leaves no rows"):
            parse_lane_budget(SITE["llm_lane_budget"], row_cap=1_000)

    def test_load_lanes_reads_the_row_cap_beside_the_site_config(self, tmp_path):
        config = tmp_path / "config"
        config.mkdir()
        site = {
            "llm_lane_budget": SITE["llm_lane_budget"],
            "llm_lanes": {"a": self._entry(), "b": self._entry(reservation_ratio=3)},
        }
        (config / "site_config.yml").write_text(yaml.safe_dump(site), encoding="utf-8")
        (config / "dispatch_tuning.yml").write_text(
            "DO_ROWS_ENQUEUE_STOP: 19500\n", encoding="utf-8"
        )
        clear_cache()
        try:
            lanes = load_lanes(path=config / "site_config.yml")
        finally:
            clear_cache()
        # (19,500 - 1,500) / (4 x 18 rows) = 250 jobs per ratio point (no live lanes).
        assert _jobs(lanes["a"]) == 250
        assert _jobs(lanes["b"]) == 750
        assert lanes["a"].daily_write_units == 1000 * 4
