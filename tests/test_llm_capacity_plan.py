"""Capacity must account for batches, shadow jobs and pinned candidate/panel fan-out."""

from scripts.llm_capacity_plan import consensus_plan, judging_demand, project


def test_full_plan_is_not_800_jobs_per_lane():
    report = project()
    assert report["new_jobs"] == 20800
    assert report["provider_attempts"] == 22880
    assert report["ingress_units"] == 94400
    assert report["billed_rows_estimate"] == 513160
    assert not report["fits_shared_caps"]
    judge = next(row for row in report["lanes"] if row["purpose"] == "r6-judge")
    assert judge["jobs"] == 12000
    assert judge["ingress_units_per_job"] == 4
    assert judge["configured_daily_job_ceiling"] == 4000
    assert judge["configured_daily_job_ceiling"] < judge["jobs"]


def test_optional_and_ineligible_work_can_be_excluded():
    report = project(
        chapter_fraction=0,
        tag_fraction=0,
        moment_fraction=1,
        quotes=0,
        shadow=False,
        retry_fraction=0,
    )
    assert report["new_jobs"] == 800
    assert report["fits_shared_caps"]


def test_panel_scales_per_quote_and_shadow_per_batch():
    report = project(quotes=10, prelabel_batches=4)
    rows = {row["purpose"]: row for row in report["lanes"]}
    assert rows["r6-judge"]["jobs"] == 24000
    assert rows["topic-tags:prelabeler-shadow"]["jobs"] == 3200
    assert report["new_jobs"] == 34400


def test_consensus_packing_makes_the_800_target_fit_on_paper():
    packed = consensus_plan()
    episode = consensus_plan(packed=False)
    assert packed["new_jobs"] == 2473
    assert packed["billed_rows_estimate"] == 71943
    assert packed["ingress_units"] == 13048
    assert episode["new_jobs"] == 3415
    assert episode["billed_rows_estimate"] == 95125
    assert episode["ingress_units"] == 17195
    assert packed["proposed_not_deployed"]
    locator = next(row for row in packed["lanes"] if row["purpose"] == "chapter-locator")
    assert locator["indexes"] == 1


def test_current_and_consensus_plans_share_operational_allowances():
    from scripts.llm_capacity_plan import IDLE_CRON_ROWS_PER_DAY, OPERATIONAL_ROWS_PER_DAY

    current = project(retry_fraction=0)
    planned = consensus_plan()
    overhead = IDLE_CRON_ROWS_PER_DAY + OPERATIONAL_ROWS_PER_DAY
    assert (
        current["billed_rows_estimate"]
        - sum(lane["first_try_rows_estimate"] for lane in current["lanes"])
        == overhead
    )
    assert (
        planned["billed_rows_estimate"] - sum(lane["rows"] for lane in planned["lanes"]) == overhead
    )


def test_capacity_budget_tracks_worker_defaults(tmp_path, monkeypatch):
    import shutil

    from scripts import llm_capacity_plan as calculator

    source_root = calculator.ROOT
    config = tmp_path / "config"
    config.mkdir()
    for filename in ("site_config.yml", "dispatch_tuning.yml"):
        shutil.copyfile(source_root / "config" / filename, config / filename)
    worker = tmp_path / "workers/llm-dispatch-v2/src"
    worker.mkdir(parents=True)
    (worker / "write_budget.js").write_text(
        "export const DO_ROWS_WRITTEN_PLATFORM_LIMIT = 100000;\n"
        "export const DO_ROWS_ACCOUNT_RESERVE = 60000;\n"
    )
    (worker / "coordinator.js").write_text('_envInt("DO_ROWS_ENQUEUE_STOP", 90000)')
    monkeypatch.setattr(calculator, "ROOT", tmp_path)
    report = project(chapter_fraction=0, tag_fraction=0, quotes=0, shadow=False, retry_fraction=0)
    assert report["safe_row_budget"] == 40000
    assert not report["fits_shared_caps"]
    assert consensus_plan()["safe_row_budget"] == 40000
    (worker / "coordinator.js").write_text('_envInt("DO_ROWS_ENQUEUE_STOP", 30000)')
    assert calculator._safe_row_budget() == 30000


def test_judging_demand_reproduces_the_review_53_reservation_table():
    demand = judging_demand()
    assert demand["tag_subjects"] == 7200 and demand["moment_meetings"] == 80
    assert 180 <= demand["packets"]["judge:anchor"] <= 200
    assert 450 <= demand["packets"]["judge:sibling"] <= 500
    # review/53 rounds these up to 920 and 2,400 reserved units.
    assert demand["reserved_write_units"]["judge:anchor"] <= 920
    assert demand["reserved_write_units"]["judge:sibling"] <= 2400
    # The JEV account (about 1,440 successful calls a day) carries the routine anchor load easily.
    assert demand["packets"]["judge:anchor"] < 1440 / 4


def test_judging_demand_scales_with_meetings_and_escalation():
    base = judging_demand()["packets"]["judge:anchor"]
    assert judging_demand(meetings=1600)["packets"]["judge:anchor"] > 1.9 * base
    assert judging_demand(escalation_share=0.5)["packets"]["judge:anchor"] > base
