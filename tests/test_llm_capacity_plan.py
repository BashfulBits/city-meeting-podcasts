"""Capacity must account for batches, shadow jobs and pinned candidate/panel fan-out."""

from scripts.llm_capacity_plan import consensus_plan, project


def test_full_plan_is_not_800_jobs_per_lane():
    report = project()
    assert report["new_jobs"] == 20800
    assert report["provider_attempts"] == 22880
    assert report["ingress_units"] == 94400
    assert report["billed_rows_estimate"] == 489760
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
    assert packed["billed_rows_estimate"] == 65631
    assert packed["ingress_units"] == 13048
    assert episode["new_jobs"] == 3415
    assert episode["billed_rows_estimate"] == 88055
    assert episode["ingress_units"] == 17195
    assert packed["proposed_not_deployed"]
    locator = next(row for row in packed["lanes"] if row["purpose"] == "chapter-locator")
    assert locator["indexes"] == 1
