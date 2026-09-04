"""The LLM dispatch lane registry: config validation, and the pins that guard the catalog.

`config/site_config.yml`'s `llm_lanes` block is the single source of truth for both halves of the
dispatch contract -- which models a lane may use, and what it may spend at the v2 ingress Worker.
These tests cover the three things that can go wrong with that arrangement:

1. A malformed or self-contradictory lane entry is accepted.
2. A dispatching purpose exists in code with no lane entry (or vice versa), which is how the
   deployed reservation map came to reserve 10,000 daily write units under `topic-tags` and
   `moments` while the client sent `topic-tags:tagger`, `topic-tags:prelabeler`, `r6-moments`, and
   `r6-judge` -- capacity withheld from every real lane and usable by none.
3. A model string that feeds a recipe hash changes, silently re-queueing the whole catalog.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from citypods.compute.llm_lanes import (
    NON_DISPATCHING_PURPOSES,
    LaneConfig,
    UnregisteredLaneError,
    clear_cache,
    lane_for,
    load_lanes,
    parse_lanes,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_CONFIG = REPO_ROOT / "config" / "site_config.yml"
COMPILED_RESERVATIONS = (
    REPO_ROOT / "workers" / "llm-dispatch-v2" / "src" / "ingress_reservations.json"
)

# Every LLMRequestPolicy.purpose in the codebase that can reach the ingress Worker. Kept as an
# explicit list rather than derived by grepping: the point is that adding a dispatching call site
# forces a deliberate edit here and in config, not that a regex silently keeps up.
DISPATCHING_PURPOSES = frozenset(
    {
        "chapter-agenda",
        "chapter-locator",
        "topic-tags:tagger",
        "topic-tags:prelabeler",
        "r6-moments",
        "r6-judge",
        "tournament:tag",
        "tournament:tag-judge",
        "r5-benchmark:tag",
        "r5-benchmark:judge",
    }
)


def _lane(**overrides):
    entry = {"models": ["m1"], "max_dispatches_per_run": 10, "daily_write_units": 100}
    entry.update(overrides)
    return {"a-purpose": entry}


class TestParsing:
    def test_accepts_a_well_formed_lane(self):
        lanes = parse_lanes(_lane())
        assert lanes["a-purpose"] == LaneConfig(
            purpose="a-purpose",
            models=("m1",),
            max_dispatches_per_run=10,
            reserved_write_units=0,
            daily_write_units=100,
            dispatch_shape="pooled",
        )

    @pytest.mark.parametrize("block", [None, {}, [], "nope"])
    def test_rejects_a_missing_or_non_mapping_block(self, block):
        with pytest.raises(ValueError, match="llm_lanes"):
            parse_lanes(block)

    def test_rejects_empty_models(self):
        with pytest.raises(ValueError, match="non-empty list"):
            parse_lanes(_lane(models=[]))

    @pytest.mark.parametrize("model", [None, 3, "", "   ", ["nested"]])
    def test_rejects_a_model_that_is_not_a_non_empty_string(self, model):
        # `str(model)` would have produced the route names "None", "3" and "", none of which any
        # llm_routes.json entry can match. Such a lane compiles and admits jobs at ingress, and
        # every one is then indexed under the `__unroutable__` sentinel and never claimed -- quota
        # spent on work that can never run.
        with pytest.raises(ValueError, match="non-empty string"):
            parse_lanes(_lane(models=[model]))

    def test_strips_surrounding_whitespace_from_models(self):
        assert parse_lanes(_lane(models=[" m1 "]))["a-purpose"].models == ("m1",)

    def test_rejects_duplicate_models(self):
        # A duplicate would double-count the lane's per-job ingress write units against its budget.
        with pytest.raises(ValueError, match="duplicates"):
            parse_lanes(_lane(models=["m1", "m1"]))

    def test_rejects_a_reservation_larger_than_its_own_daily_cap(self):
        # Reserved units are subtracted from every OTHER lane's usable headroom, so a reservation
        # the lane itself can never spend is capacity destroyed rather than merely misallocated.
        with pytest.raises(ValueError, match="never spend"):
            parse_lanes(_lane(reserved_write_units=200, daily_write_units=100))

    def test_rejects_a_budget_too_small_for_one_job(self):
        with pytest.raises(ValueError, match="never admit a single job"):
            parse_lanes(_lane(daily_write_units=2))

    def test_rejects_an_unknown_dispatch_shape(self):
        with pytest.raises(ValueError, match="dispatch_shape"):
            parse_lanes(_lane(dispatch_shape="broadcast"))

    @pytest.mark.parametrize("field", ["max_dispatches_per_run", "daily_write_units"])
    def test_rejects_non_integer_budgets(self, field):
        with pytest.raises(ValueError, match=field):
            parse_lanes(_lane(**{field: "lots"}))

    def test_rejects_a_run_cap_above_what_the_daily_budget_funds(self):
        # Not a harmless ceiling: the producer submits to the run cap and the Worker rejects
        # everything past the daily budget, so the surplus is wasted ingress traffic that reads
        # as a lane failing to fill its quota. Four lanes shipped with this skew.
        with pytest.raises(ValueError, match="rejected at ingress"):
            parse_lanes(_lane(max_dispatches_per_run=100, daily_write_units=100))

    def test_rejects_registering_a_non_dispatching_purpose(self):
        with pytest.raises(ValueError, match="never reaches the ingress Worker"):
            parse_lanes({"topic-tags:rules": _lane()["a-purpose"]})


class TestWriteUnitAccounting:
    def test_a_pooled_lane_charges_one_index_row_per_model(self):
        # Mirrors coordinator.js's _ingressWriteUnitsFor: job row + purpose ledger + scheduler
        # counter + one model-index row per allowed model.
        lane = parse_lanes(_lane(models=["m1", "m2", "m3"]))["a-purpose"]
        assert lane.ingress_write_units_per_job == 6

    def test_a_per_model_lane_charges_one_index_row_regardless_of_model_count(self):
        # A per_model lane fans out one single-route job per model, so any given job indexes
        # exactly one. Charging it as if each job indexed all of them would over-reserve the
        # lane's budget by ~75% at four contestants.
        block = _lane(models=["m1", "m2", "m3"], dispatch_shape="per_model")
        lane = parse_lanes(block)["a-purpose"]
        assert lane.ingress_write_units_per_job == 4

    def test_max_jobs_per_day_floors_rather_than_stranding_a_partial_job(self):
        lane = parse_lanes(_lane(models=["m1"], daily_write_units=99))["a-purpose"]
        assert lane.ingress_write_units_per_job == 4
        assert lane.max_jobs_per_day == 24  # 99 // 4, not 24.75


class TestLaneLookup:
    def test_unregistered_purpose_raises_with_an_actionable_message(self):
        with pytest.raises(UnregisteredLaneError) as excinfo:
            lane_for("topic-tags:summarizer")
        message = str(excinfo.value)
        assert "llm_lanes" in message
        assert "scripts/compile_llm_lanes.py" in message

    def test_a_sub_purpose_does_not_inherit_its_prefix(self):
        # "topic-tags" is the OLD reservation key. It must not resolve just because
        # "topic-tags:tagger" exists -- prefix inheritance is exactly the bug being fixed.
        with pytest.raises(UnregisteredLaneError):
            lane_for("topic-tags")

    @pytest.mark.parametrize("purpose", sorted(NON_DISPATCHING_PURPOSES))
    def test_non_dispatching_purposes_are_refused_explicitly(self, purpose):
        # `topic-tags:rules` makes no LLM call; `city-onboarding` sets require_direct=True. Asking
        # for a dispatch lane for either is a bug in the caller, and the error says which.
        with pytest.raises(UnregisteredLaneError, match="non-dispatching"):
            lane_for(purpose)

    def test_lane_resolution_takes_no_per_run_site_config(self):
        # The registry is repository-level policy, and deliberately has no per-run override.
        # `scripts/compile_llm_lanes.py` builds the Worker's reservation map from the committed
        # `config/site_config.yml` alone, so a run that resolved its models or budgets from some
        # other config would submit jobs the deployed Worker either rejects outright
        # (`purpose_not_registered`) or charges against budgets that config never described: the
        # override would appear to work in the producer and simply not exist at ingress. A
        # selected `--site-config` still governs site content, and still NARROWS a lane where that
        # is meaningful (`moments.judges.models` picks a subset of `llm_lanes["r6-judge"]`).
        with pytest.raises(TypeError):
            lane_for("topic-tags:tagger", {"llm_lanes": {}})  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            load_lanes({"llm_lanes": {}})  # type: ignore[call-arg]

    def test_lane_lookup_ignores_the_process_working_directory(self, tmp_path, monkeypatch):
        # Several lanes resolve their models at import time to build a recipe hash. A lookup that
        # silently found no config because a tool ran from a subdirectory would change those
        # hashes and re-queue the catalog.
        monkeypatch.chdir(tmp_path)
        # Without this the assertion can be satisfied from a cache entry populated by an earlier
        # test, which would pass whether or not the lookup is actually CWD-independent.
        clear_cache()
        assert lane_for("chapter-agenda").primary_model


class TestRepositoryConfig:
    def test_every_dispatching_purpose_has_a_lane(self):
        assert DISPATCHING_PURPOSES <= set(load_lanes())

    def test_no_lane_exists_for_a_purpose_nothing_dispatches(self):
        # A stale lane is not harmless: its reservation is subtracted from every other lane's
        # headroom. This is the assertion that would have caught the original bug.
        assert set(load_lanes()) <= DISPATCHING_PURPOSES

    def test_reservations_fit_the_workers_global_ingress_budget(self):
        compiled = json.loads(COMPILED_RESERVATIONS.read_text())
        assert compiled["reserved_total"] <= compiled["global_write_budget"]

    def test_compiled_map_matches_the_yaml(self):
        # The deploy workflow runs `compile_llm_lanes.py --check`; this fails the same drift in the
        # ordinary test suite so it is caught before a deploy job rejects it.
        compiled = json.loads(COMPILED_RESERVATIONS.read_text())
        lanes = load_lanes()
        assert set(compiled["reservations"]) == set(lanes)
        for purpose, lane in lanes.items():
            entry = compiled["reservations"][purpose]
            assert entry["reserved_write_units"] == lane.reserved_write_units
            assert entry["daily_write_units"] == lane.daily_write_units
            assert entry["models"] == list(lane.models)

    def test_every_lane_run_cap_is_funded_by_its_daily_budget(self):
        for lane in load_lanes().values():
            assert lane.max_dispatches_per_run <= lane.max_jobs_per_day, lane.purpose

    def test_site_config_no_longer_carries_duplicate_model_lists(self):
        # These keys moved into `llm_lanes`. Leaving a copy behind is how the two lists drifted
        # apart in the first place, so their absence is part of the contract.
        site = yaml.safe_load(SITE_CONFIG.read_text())
        assert "llm_models" not in site["tagging"]
        assert "llm_model" not in site["tagging"]
        assert "models" not in site["moments"]["judges"]
        assert "model" not in site["tagging"]["prelabeler"]


class TestRecipeAffectingModelPins:
    """These strings are inputs to recipe hashes; changing one re-queues the whole catalog.

    A change here is a deliberate backfill whose story the PR and CHANGELOG must state (AGENTS.md,
    "Pipeline-version bumps state their backfill story"). Failing here is the intended way to find
    that out -- before weeks of catalog rework is queued, not after.
    """

    @pytest.mark.parametrize(
        ("purpose", "expected"),
        [
            ("chapter-agenda", "mistral/mistral-medium-latest"),
            ("chapter-locator", "gemini/gemini-3.5-flash-lite"),
            ("topic-tags:tagger", "gemini/gemini-3.1-flash-lite"),
            ("topic-tags:prelabeler", "google/gemma-4-31b-it"),
        ],
    )
    def test_production_route_is_pinned(self, purpose, expected):
        assert lane_for(purpose).primary_model == expected

    def test_constants_resolve_to_their_lane(self):
        from citypods.chapter_locator import PRODUCTION_LOCATOR_MODEL
        from citypods.chapter_titles import AGENDA_PRODUCTION_MODEL, AGENDA_PRODUCTION_MODELS

        assert AGENDA_PRODUCTION_MODEL == lane_for("chapter-agenda").primary_model
        assert AGENDA_PRODUCTION_MODELS == lane_for("chapter-agenda").models
        assert PRODUCTION_LOCATOR_MODEL == lane_for("chapter-locator").primary_model


class TestTournamentGrid:
    def test_contest_grid_covers_every_unordered_contestant_pair(self):
        from citypods.tournament import CONTESTS, JUDGE_MODEL, MODELS

        pairs = {frozenset((left, right)) for left, right, _judge in CONTESTS}
        expected = {
            frozenset((left, right))
            for index, left in enumerate(MODELS)
            for right in MODELS[index + 1 :]
        }
        assert pairs == expected
        assert len(CONTESTS) == len(MODELS) * (len(MODELS) - 1) // 2
        assert all(judge == JUDGE_MODEL for _l, _r, judge in CONTESTS)

    def test_judge_is_never_also_a_contestant(self):
        # `_build_pairwise_judge_job` raises on this; catching it in config is cheaper than
        # discovering it mid-run.
        from citypods.tournament import JUDGE_MODEL, MODELS

        assert JUDGE_MODEL not in MODELS


class TestJudgePanel:
    def test_absent_or_empty_allowlist_uses_the_whole_configured_panel(self):
        from citypods.moment_judging import JUDGE_MODELS, judge_models

        # MomentJudgeStage reads `judges.models` from site config, which no longer carries a list,
        # so the empty case is the normal one. Treating it as an empty intersection would disable
        # judging with no error anywhere.
        assert judge_models(None) == JUDGE_MODELS
        assert judge_models(()) == JUDGE_MODELS
        assert judge_models([]) == JUDGE_MODELS

    def test_a_matching_allowlist_narrows_the_panel(self):
        from citypods.moment_judging import JUDGE_MODELS, judge_models

        assert judge_models([JUDGE_MODELS[0]]) == (JUDGE_MODELS[0],)

    def test_a_non_matching_allowlist_is_an_error_not_a_silent_disable(self):
        from citypods.moment_judging import judge_models

        with pytest.raises(ValueError, match="matches none of"):
            judge_models(["retired/typo-route"])


class TestTournamentSampleBudget:
    def test_budget_respects_both_lanes_and_fits_each(self):
        from citypods.tournament import CONTESTS, MODELS

        tag = lane_for("tournament:tag")
        judge = lane_for("tournament:tag-judge")
        # One sample spends from two independent lane budgets: len(MODELS) candidate jobs against
        # tournament:tag and len(CONTESTS)*2 comparisons against tournament:tag-judge. Dividing
        # either budget by the COMBINED count would overrun the other lane.
        samples = min(
            tag.max_dispatches_per_run // len(MODELS),
            judge.max_dispatches_per_run // (len(CONTESTS) * 2),
        )
        assert samples >= 1
        assert samples * len(MODELS) <= tag.max_dispatches_per_run
        assert samples * len(CONTESTS) * 2 <= judge.max_dispatches_per_run
