"""Purpose tracking enforces producer ownership without changing recipes or payloads."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import BatchingDispatchBackend, LiteLLMBackend, LLMBackendConfig
from citypods.compute.llm_lanes import parse_lanes
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.compute.llm_work import LLMWorkTracker, validate_work_binding, write_run_event
from citypods.ops import backlog_trend as trend
from tests._cas_fake import MemStorage


def tracker():
    return LLMWorkTracker(
        parse_lanes(
            {
                "new-purpose": {
                    "models": ["example/model"],
                    "max_dispatches_per_run": 1,
                    "daily_write_units": 100,
                    "telemetry": {
                        "producer": "new-stage",
                        "unit": "document",
                        "completion": "consumed",
                        "scope": "retained_catalog",
                    },
                }
            }
        )
    )


def job(purpose="new-purpose", recipe="recipe"):
    return InferenceJob(
        "tag",
        {
            "messages": [],
            "llm_policy": LLMRequestPolicy(
                purpose=purpose, allowed_models=("example/model",), queue_only=True
            ),
        },
        recipe,
    )


def row(work):
    return work.snapshot()["purposes"]["new-purpose"]


def test_registration_requires_telemetry_and_owner():
    work = tracker()
    with pytest.raises(ValueError, match="belongs"):
        work.item("new-purpose", "unit", producer="wrong")
    with pytest.raises(ValueError, match="Unregistered"):
        work.item("retired", "unit", producer="new-stage")
    with pytest.raises(ValueError, match="telemetry"):
        parse_lanes(
            {"new": {"models": ["m"], "max_dispatches_per_run": 1, "daily_write_units": 100}}
        )


def test_pre_submission_cap_and_replay_do_not_inflate_work():
    work = tracker()

    def register(_):
        work.item("new-purpose", "private-uid", producer="new-stage").defer("ingress_limited")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(register, range(100)))
    assert row(work)["observed"] == row(work)["backlog"] == 1
    item = work.item("new-purpose", "private-uid", producer="new-stage")
    item.result(job(), JobResult("tag", {}, "recipe"))
    assert row(work)["consumed"] == 0  # A response can still fail local finalization.
    item.defer("errored")
    assert row(work)["states"] == {"errored": 1}
    item.consumed()
    item.consumed(reused=True)
    item.defer("stopped")  # A replay's late stop must not undo successful consumption.
    assert row(work)["consumed"] == 1
    assert row(work)["backlog"] == 0
    assert "private-uid" not in str(work.snapshot())
    assert "recipe" not in str(work.snapshot())


def test_unbound_and_mismatched_jobs_fail_before_backend_io():
    work = tracker()
    backend = LiteLLMBackend(LLMBackendConfig(model="gemini/gemini-3.1-flash-lite"))
    with work.producer("new-stage"):
        with pytest.raises(ValueError, match="purpose-bound"):
            backend.run_inference(job())
        with pytest.raises(ValueError, match="purpose-bound"):
            backend.enqueue_batch([job()])
    item = work.item("new-purpose", "unit", producer="new-stage")
    with pytest.raises(ValueError, match="does not match"):
        item.bind(job("wrong"))
    with work.producer("wrong"):
        with pytest.raises(ValueError, match="different producer"):
            validate_work_binding(item.bind(job()))


def test_partial_census_and_cached_reuse_cannot_count_consumption(tmp_path):
    work = tracker()
    item = work.item("new-purpose", "unit", producer="new-stage")
    item.consumed(reused=True)
    with pytest.raises(RuntimeError), work.producer("new-stage"):
        raise RuntimeError("interrupted producer")
    path = write_run_event(work, tmp_path, "new-stage")
    assert (tmp_path / path).exists()
    assert row(work)["coverage"] == "partial"
    assert row(work)["consumed"] == 0


def test_flush_records_failure_for_bound_provisional_job():
    work = tracker()

    class Backend:
        name = "fake"
        config = SimpleNamespace(model="example/model", dispatch_v2_url="https://test")
        storage = MemStorage()

        def enqueue_batch(self, jobs):
            return [RuntimeError("submission failed") for _ in jobs]

    backend = BatchingDispatchBackend(Backend())
    item = work.item("new-purpose", "unit", producer="new-stage")
    result = item.backend(backend).run_inference(job())
    assert isinstance(result, JobHandle)
    assert row(work)["job_outcomes"] == {"prepared": 1}
    backend.flush()
    assert row(work)["job_outcomes"] == {"errored": 1}
    assert row(work)["states"] == {"errored": 1}


def explicit(work, day=1, **scope):
    work.finish()
    return {
        "ts": f"2026-10-{day:02d}T00:00:00+00:00",
        "outcome": "completed",
        "stages": {},
        "llm_work": work.snapshot(),
        **scope,
    }


def test_new_and_retired_purposes_need_no_reader_mapping():
    work = tracker()
    work.item("new-purpose", "unit", producer="new-stage").defer("ingress_limited")
    events = [explicit(work)]
    specs, tokens, lifecycle = trend.discover_verbs(events, {"llm_lanes": {}})
    assert lifecycle["new-purpose"] == "retired"
    report = trend.analyze(events, "new-purpose", trend.BacklogParams(), specs=specs, tokens=tokens)
    assert report.backlog == 1
    assert report.days == 1
    assert report.throughput_per_day is None


def test_shards_require_complete_coverage_and_do_not_overlap_full_census():
    work = tracker()
    work.item("new-purpose", "unit", producer="new-stage").defer("queued")
    partial = [explicit(work, shard="0/2", scoped=True)]
    params = trend.BacklogParams()
    assert trend.purpose_points(partial, "new-purpose", params) == []
    full_shards = partial + [explicit(work, shard="1/2", scoped=True)]
    assert trend.purpose_points(full_shards, "new-purpose", params)[0].backlog == 2
    assert (
        trend.purpose_points(full_shards + [explicit(work, scoped=True)], "new-purpose", params)[
            0
        ].backlog
        == 1
    )
    work.partial("new-stage")
    assert trend.purpose_points([explicit(work)], "new-purpose", params) == []


def test_registered_stage_requires_census_even_with_no_dirty_work(tmp_path):
    from citypods.models import City
    from citypods.stages import StageContext, run_stages

    ctx = StageContext(storage=None, ffmpeg=None, max_kbps=96, dry_run=True, llm_work=tracker())
    city = City(
        slug="test",
        provider="test",
        source={},
        podcast_title="test",
        podcast_author="test",
        podcast_email="",
        podcast_description="",
    )
    stage = SimpleNamespace(name="new-stage")
    with pytest.raises(ValueError, match="eligibility census"):
        run_stages(None, city, [], [stage], ctx)


def test_two_purposes_in_one_stage_keep_work_and_consumption_separate():
    work = LLMWorkTracker()
    tag = work.item("topic-tags:tagger", "same-episode", producer="tags")
    prelabel = work.item("topic-tags:prelabeler", "same-episode", producer="tags")
    shadow = work.item("topic-tags:prelabeler-shadow", "same-episode", producer="tags")
    tag.consumed(reused=True)
    prelabel.consumed()
    shadow.defer("ingress_limited")
    snapshot = work.snapshot()["purposes"]
    assert snapshot["topic-tags:tagger"]["consumed"] == 0
    assert snapshot["topic-tags:prelabeler"]["consumed"] == 1
    assert snapshot["topic-tags:prelabeler-shadow"]["backlog"] == 1


def test_existing_run_event_channel_preserves_purpose_snapshot(tmp_path):
    import json

    from citypods.run import _record_run_history

    work = tracker()
    work.activate("new-purpose", producer="new-stage")
    work.finish()
    relative = _record_run_history(
        tmp_path, [], {}, scope={"scoped": True}, llm_work=work.snapshot()
    )
    event = json.loads((tmp_path / relative).read_text())
    assert event["llm_work"] == work.snapshot()
    assert event["llm_work"]["purposes"]["new-purpose"]["observed"] == 0


def test_report_does_not_recommend_from_old_data_after_partial_newest_run(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime

    import yaml

    work = tracker()
    for index in range(20):
        work.item("new-purpose", str(index), producer="new-stage").defer("ingress_limited")
    events = [explicit(work, day=day) for day in range(1, 6)]
    work.partial("new-stage")
    events.append(explicit(work, day=6))
    directory = tmp_path / "run_events"
    directory.mkdir()
    for index, event in enumerate(events):
        (directory / f"{index}.json").write_text(json.dumps(event))
    site = tmp_path / "site.yml"
    site.write_text(yaml.safe_dump({"llm_lanes": {"new-purpose": {}}}))
    monkeypatch.setattr(trend, "_now", lambda: datetime(2026, 10, 6, 12, tzinfo=UTC))
    output = tmp_path / "report.json"
    assert (
        trend.main(
            ["--state-dir", str(tmp_path), "--site-config", str(site), "--json", str(output)]
        )
        == 0
    )
    report = json.loads(output.read_text())["verbs"]["new-purpose"]
    assert report["backlog"] is None
    assert report["coverage"] == "partial"
    assert report["action"] is None
    assert not report["constrained"]


def test_equal_timestamp_shard_events_do_not_compare_payload_dicts():
    work = tracker()
    work.item("new-purpose", "unit", producer="new-stage").defer("queued")
    events = [explicit(work, shard="0/2"), explicit(work, shard="0/2"), explicit(work, shard="1/2")]
    assert trend.purpose_points(events, "new-purpose", trend.BacklogParams())[0].backlog == 2
    assert trend.purpose_points([], "new-purpose", trend.BacklogParams()) == []
