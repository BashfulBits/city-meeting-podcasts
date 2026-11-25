"""Independent search publication preserves complete output on deferred indexing."""

import json
from pathlib import Path

import pytest
import yaml

from citypods import cli, search


def _site(tmp_path, monkeypatch, *, enabled=True, budget=20):
    output = tmp_path / "docs"
    output.mkdir()
    for name, body in {
        "index.html": "old navigation",
        "meta.json": '{"built": 7}',
        "data/search/old.json": "old shard",
        "data/search/manifest.json": "old manifest",
        "search/index.html": "old page",
        "assets/minisearch-7.1.2.js": "old asset",
        "raw/index.html": "raw retained",
    }.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"feed_info": {}, "base_url": "https://example.test"}))
    monkeypatch.setattr(
        "citypods.config.load_site_config",
        lambda _: {
            "defaults": {"search": enabled, "search_index_budget_minutes": budget},
        },
    )
    monkeypatch.setattr("citypods.config.load_city_configs", lambda *_: [])
    monkeypatch.setattr("citypods.storage.make_storage", lambda *_: None)
    monkeypatch.setattr("citypods.site.render_index", lambda *_, **kw: str(kw["search_enabled"]))
    monkeypatch.setattr("citypods.site.render_search_page", lambda *_: "new page")
    return output, dict(state_dir=tmp_path / "state", output_dir=output, context_path=context)


def _bytes(output):
    return {str(p.relative_to(output)): p.read_bytes() for p in output.rglob("*") if p.is_file()}


@pytest.mark.parametrize("failure", [False, True])
def test_partial_or_error_preserves_every_public_byte(tmp_path, monkeypatch, failure):
    output, args = _site(tmp_path, monkeypatch)
    prior = _bytes(output)

    def partial(_state, _cities, staged, *_args, **_kwargs):
        (staged / "data/search/old.json").write_text("partially overwritten")
        if failure:
            raise RuntimeError("second source failed")
        return None

    monkeypatch.setattr(search, "build_search_index", partial)
    assert search.build_search_site(**args).startswith("deferred")
    assert _bytes(output) == prior


def test_complete_publishes_staged_search_and_preserves_raw_meta(tmp_path, monkeypatch):
    output, args = _site(tmp_path, monkeypatch)

    def complete(_state, _cities, staged, *_args, **kwargs):
        assert not kwargs["stop"]()
        (staged / "data/search/old.json").unlink()
        (staged / "data/search/new.json").write_text("new shard")
        return {"shards": [{"source_key": "new"}]}

    monkeypatch.setattr(search, "build_search_index", complete)
    assert search.build_search_site(**args) == "complete"
    assert not (output / "data/search/old.json").exists()
    assert (output / "raw/index.html").read_text() == "raw retained"
    assert json.loads((output / "meta.json").read_text()) == {"built": 7, "search_shards": 1}
    assert (output / "index.html").read_text() == "True"
    assert not Path(args["state_dir"]).exists()


def test_disabled_search_removes_search_without_indexing(tmp_path, monkeypatch):
    output, args = _site(tmp_path, monkeypatch, enabled=False)
    monkeypatch.setattr(search, "build_search_index", lambda *_a, **_k: pytest.fail("indexing"))
    assert search.build_search_site(**args) == "disabled"
    assert not (output / "search").exists()
    assert (output / "raw/index.html").read_text() == "raw retained"


@pytest.mark.parametrize("budget", [0, -1, 121])
def test_budget_ceiling(tmp_path, monkeypatch, budget):
    output, args = _site(tmp_path, monkeypatch, budget=budget)
    prior = _bytes(output)
    with pytest.raises(ValueError, match="at most 120"):
        search.build_search_site(**args)
    assert _bytes(output) == prior


def test_cli_wires_readonly_command_and_rejects_wrong_phase(monkeypatch):
    calls = []
    monkeypatch.setattr(search, "build_search_site", lambda **kw: calls.append(kw) or "complete")
    assert cli.main(["search-index", "--context-path", "same-run.json"]) == 0
    assert calls[0]["context_path"] == "same-run.json"
    with pytest.raises(SystemExit):
        cli.main(["build", "--skip-search"])


def test_deploy_workflow_publishes_the_render_without_waiting_for_search():
    workflow = yaml.safe_load(Path(".github/workflows/deploy.yml").read_text())
    jobs = workflow["jobs"]
    # Search builds in its own workflow: a 2h index build must never gate a merge-driven publish.
    assert set(jobs) == {"render", "deploy"}
    assert jobs["deploy"]["needs"] == "render"
    assert workflow["concurrency"] == {"group": "pages", "cancel-in-progress": False}
    # The one Actions-API use is the cross-run staged-index download, scoped to the render job.
    assert jobs["render"]["permissions"] == {
        "contents": "read",
        "pages": "read",
        "actions": "read",
    }
    assert "actions" not in (workflow.get("permissions") or {})
    inputs = (workflow.get("on") or workflow.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["search_run_id"]["type"] == "string"
    assert inputs["search_run_id"]["required"] is False
    # The index is resolved from the search workflow, not trusted from the evictable `docs` cache.
    resolve = next(
        s for s in jobs["render"]["steps"] if s.get("name") == "Resolve search index run"
    )
    assert "gh run list --workflow search.yml" in resolve["run"]
    # Dispatch input travels via env, never inlined into shell text.
    assert resolve["env"]["SEARCH_RUN_ID"] == "${{ inputs.search_run_id }}"
    assert "$SEARCH_RUN_ID" in resolve["run"]
    staged = next(
        s for s in jobs["render"]["steps"] if s.get("name") == "Stage completed search index"
    )
    assert staged["with"]["name"] == "search-index"
    assert staged["with"]["path"] == "docs"
    assert "steps.index.outputs.run_id" in staged["if"]
    # Staged *before* the render, which is what makes the entry point and shard counts match.
    names = [s.get("name") for s in jobs["render"]["steps"]]
    assert names.index("Stage completed search index") < names.index("Render feeds")
    pages = next(s for s in jobs["render"]["steps"] if s.get("name") == "Configure Pages")
    assert pages["with"]["enablement"] is False
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert jobs["render"]["timeout-minutes"] == jobs["deploy"]["timeout-minutes"] == 60
    handoff = next(s for s in jobs["render"]["steps"] if s.get("name") == "Upload render handoff")
    assert ".citypods-state/sources/*/episodes.json" in handoff["with"]["path"]
    assert handoff["with"]["include-hidden-files"] is True
    # The handoff carries `docs/` plus search inputs, so it lands in a scratch dir and only
    # `staging/docs` is published.
    site = next(
        s for s in jobs["deploy"]["steps"] if s.get("name") == "Download same-run rendered site"
    )
    assert site["with"]["name"] == "render-search-input"
    assert site["with"]["path"] == "staging"
    artifact = next(s for s in jobs["deploy"]["steps"] if s.get("name") == "Upload Pages artifact")
    assert artifact["with"]["path"] == "staging/docs"


def test_search_workflow_stages_the_index_and_requests_a_publish():
    path = Path(".github/workflows/search.yml")
    workflow = yaml.safe_load(path.read_text())
    jobs = workflow["jobs"]
    # One publisher only: deploy.yml owns the Pages deployment, so search stages and dispatches.
    assert set(jobs) == {"build"}
    assert "deploy-pages" not in path.read_text()
    assert "upload-pages-artifact" not in path.read_text()
    assert workflow["concurrency"] == {"group": "pages-search", "cancel-in-progress": False}
    build = jobs["build"]
    assert build["permissions"] == {"contents": "read", "pages": "read", "actions": "write"}
    # Well above the 120-minute indexing budget so the graceful deferral always wins.
    assert build["timeout-minutes"] == 180
    render = next(s for s in build["steps"] if s.get("name") == "Render feeds")
    assert "--skip-search" in render["run"] and "--search-context-output" in render["run"]
    checkpoint = next(
        s for s in build["steps"] if s.get("name") == "Restore resumable search checkpoint"
    )
    assert checkpoint["with"]["path"] == ".citypods-search-checkpoint"
    assert checkpoint["with"]["restore-keys"] == "search-checkpoint-\n"
    assert "github.run_attempt" in checkpoint["with"]["key"]
    assert "actions/cache/restore@" in checkpoint["uses"]
    # Saved explicitly: the cache action's implicit post-step save is skipped on job failure.
    saved = next(s for s in build["steps"] if s.get("name") == "Save resumable search checkpoint")
    assert "actions/cache/save@" in saved["uses"]
    assert saved["if"] == "always()"
    assert saved["with"]["key"] == checkpoint["with"]["key"]
    search = next(s for s in build["steps"] if s.get("name") == "Build bounded search")
    assert search["shell"] == "bash"  # GitHub supplies pipefail for explicitly selected Bash.
    assert "Restored checkpoint:" in search["run"]
    assert '| tee -a "$GITHUB_STEP_SUMMARY"' in search["run"]
    artifact = next(s for s in build["steps"] if s.get("name") == "Upload search index")
    assert artifact["with"]["name"] == "search-index"
    paths = artifact["with"]["path"]
    # The search payload only: the deploy run re-renders everything else from current state.
    assert "docs/data/search" in paths and "docs/search" in paths
    assert "docs/index.html" not in paths
    dispatch = next(
        s for s in build["steps"] if s.get("name") == "Request a publish that includes this index"
    )
    assert "gh workflow run deploy.yml" in dispatch["run"]
    assert "search_run_id=${GITHUB_RUN_ID}" in dispatch["run"]
    assert dispatch["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_search_workflow_schedule_matches_site_config_publish_cadence():
    workflow = yaml.safe_load(Path(".github/workflows/search.yml").read_text())
    triggers = workflow.get("on") or workflow.get(True)
    site = yaml.safe_load(Path("config/site_config.yml").read_text())
    # The 4h production publish cadence lives in both places; keep them from drifting apart.
    assert [item["cron"] for item in triggers["schedule"]] == [site["schedule_cron"]]
    assert "workflow_dispatch" in triggers


@pytest.mark.parametrize("budget", [1, 120])
def test_fresh_search_deadline_is_independent_of_render(tmp_path, monkeypatch, budget):
    _output, args = _site(tmp_path, monkeypatch, budget=budget)
    ticks = iter([1000, 1000 + budget * 60 - 1, 1000 + budget * 60])
    monkeypatch.setattr("time.monotonic", lambda ticks=ticks: next(ticks))

    def deadline(_state, _cities, _staged, *_args, **kwargs):
        assert kwargs["stop"]() is False
        assert kwargs["stop"]() is True
        return None

    monkeypatch.setattr(search, "build_search_index", deadline)
    assert search.build_search_site(**args).startswith("deferred")


def test_six_addison_archive_records_absent_from_shards_and_raw_pages_retained(
    tmp_path, monkeypatch
):
    from citypods.config import load_city_configs
    from citypods.records import save_records, source_key

    cities = [c for c in load_city_configs("config", {}) if c.city_entity == "addison-tx"]
    declarations = [d for c in cities for d in c.extra.get("archive_only", [])]
    assert len(declarations) == 6
    for city in cities:
        city.extra.pop("publication_selection", None)
    output, args = _site(tmp_path, monkeypatch)
    monkeypatch.setattr("citypods.config.load_city_configs", lambda *_: cities)
    records = {
        d["uid"]: {
            "uid": d["uid"],
            "provider_guid": d["provider_guid"],
            "title": "Archived recording",
            "body": "Public Input",
            "published": "2017-01-01T00:00:00+00:00",
            "video_url": "https://example.test/video",
            "audio_url": "https://example.test/audio",
        }
        for d in declarations
    }
    save_records(args["state_dir"], source_key(cities[0]), records)
    before = _bytes(Path(args["state_dir"]))
    assert search.build_search_site(**args) == "complete"
    documents = [
        doc
        for p in (output / "data/search").glob("*.json")
        if p.name != "manifest.json"
        for doc in json.loads(p.read_text())["documents"]
    ]
    assert not ({d["uid"] for d in declarations} & {d["uid"] for d in documents})
    assert (output / "raw/index.html").read_text() == "raw retained"
    assert _bytes(Path(args["state_dir"])) == before


def test_staged_navigation_error_retains_complete_prior_site(tmp_path, monkeypatch):
    output, args = _site(tmp_path, monkeypatch)
    before = _bytes(output)
    monkeypatch.setattr(search, "build_search_index", lambda *_a, **_k: {"shards": []})

    def broken_page(*_args):
        raise RuntimeError("template error")

    monkeypatch.setattr("citypods.site.render_search_page", broken_page)
    assert search.build_search_site(**args).startswith("deferred")
    assert _bytes(output) == before


def test_actual_second_source_stop_discards_first_staged_shard(tmp_path, monkeypatch):
    from citypods.models import City

    output, args = _site(tmp_path, monkeypatch)
    prior = _bytes(output)
    cities = [
        City(
            slug=f"city-{n}",
            provider="faketest",
            source={"url": f"https://example.test/{n}"},
            podcast_title=f"City {n}",
            podcast_author="City",
            podcast_email="",
            podcast_description="Meetings",
        )
        for n in range(2)
    ]
    monkeypatch.setattr("citypods.config.load_city_configs", lambda *_: cities)
    _seed_records(args, cities)
    ticks = iter([0, 1, 1200])
    monkeypatch.setattr("time.monotonic", lambda ticks=ticks: next(ticks))
    assert search.build_search_site(**args).startswith("deferred")
    assert _bytes(output) == prior


def test_cross_run_deferral_overlays_latest_complete_preserving_fresh_pages(tmp_path, monkeypatch):
    output, args = _site(tmp_path, monkeypatch)

    def completed(_state, _cities, staged, *_args, **kwargs):
        (staged / "data/search/manifest.json").write_text('{"shards": []}')
        (staged / "data/search/old.json").write_text("latest complete shard")
        kwargs["cache"]["progress"] = 1
        return {"shards": []}

    monkeypatch.setattr(search, "build_search_index", completed)
    assert search.build_search_site(**args) == "complete"
    # A new render restores an older site snapshot, with newly rendered non-search pages.
    (output / "data/search/old.json").write_text("old render snapshot")
    (output / "raw/index.html").write_text("fresh raw page")

    def deferred(_state, _cities, staged, *_args, **kwargs):
        assert kwargs["cache"]["progress"] == 1
        kwargs["cache"]["progress"] = 2
        (staged / "data/search/old.json").write_text("partial shard")
        return None

    monkeypatch.setattr(search, "build_search_index", deferred)
    assert search.build_search_site(**args).startswith("deferred")
    assert (output / "data/search/old.json").read_text() == "latest complete shard"
    assert (output / "raw/index.html").read_text() == "fresh raw page"
    checkpoint = tmp_path / ".citypods-search-checkpoint"
    assert json.loads((checkpoint / "working/.search-cache.json").read_text())["progress"] == 2
    assert json.loads((checkpoint / "complete/.search-cache.json").read_text())["progress"] == 1

    def resumed(_state, _cities, staged, *_args, **kwargs):
        assert kwargs["cache"]["progress"] == 2
        assert (staged / "data/search/old.json").read_text() == "partial shard"
        (staged / "data/search/manifest.json").write_text('{"shards": []}')
        return {"shards": []}

    monkeypatch.setattr(search, "build_search_index", resumed)
    assert search.build_search_site(**args) == "complete"
    assert (output / "raw/index.html").read_text() == "fresh raw page"


def test_malformed_checkpoint_does_not_delete_public_output(tmp_path, monkeypatch):
    output, args = _site(tmp_path, monkeypatch)
    checkpoint = tmp_path / ".citypods-search-checkpoint/complete/data/search"
    checkpoint.mkdir(parents=True)
    (checkpoint / "manifest.json").write_text('{"shards": "invalid"}')
    prior = _bytes(output)
    monkeypatch.setattr(search, "build_search_index", lambda *_a, **_kw: None)
    assert search.build_search_site(**args).startswith("deferred")
    assert _bytes(output) == prior


def test_actual_multiple_budgets_resume_completed_sources(tmp_path, monkeypatch):
    from citypods.models import City

    output, args = _site(tmp_path, monkeypatch)
    cities = [
        City(
            slug=f"city-{n}",
            provider="faketest",
            source={"url": f"https://example.test/{n}"},
            podcast_title=f"City {n}",
            podcast_author="City",
            podcast_email="",
            podcast_description="Meetings",
        )
        for n in range(3)
    ]
    monkeypatch.setattr("citypods.config.load_city_configs", lambda *_: cities)
    _seed_records(args, cities)
    # Each budget permits one uncached source. Cache hits must precede deadline admission.
    for completed_count in (1, 2, 3):
        ticks = iter([0, 1, 1200])
        monkeypatch.setattr("time.monotonic", lambda ticks=ticks: next(ticks))
        result = search.build_search_site(**args)
        working = tmp_path / ".citypods-search-checkpoint/working/.search-cache.json"
        assert len(json.loads(working.read_text())["shards"]) == completed_count
        assert result == "complete" if completed_count == 3 else result.startswith("deferred")
    assert len(json.loads((output / "data/search/manifest.json").read_text())["shards"]) == 3
    assert (output / "raw/index.html").read_text() == "raw retained"


def _seed_records(args, cities):
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.records import episode_to_record, save_records, source_key

    for city in cities:
        record = episode_to_record(
            Episode(
                guid="g1",
                uid="u1",
                title="Council",
                published=datetime(2026, 1, 1, tzinfo=UTC),
                video_url="https://example.test/video",
            )
        )
        save_records(args["state_dir"], source_key(city), {"u1": record})


def test_deferral_without_complete_index_is_explicit(tmp_path, monkeypatch):
    output, args = _site(tmp_path, monkeypatch)
    before = _bytes(output)
    monkeypatch.setattr(search, "build_search_index", lambda *_a, **_k: None)
    assert search.build_search_site(**args) == "deferred: no complete search index available"
    assert _bytes(output) == before


def test_bounded_artifact_cache_evicts_oldest_and_skips_oversized():
    cache = search._BoundedArtifactCache(max_bytes=10)
    cache["a"] = b"12345"
    cache["b"] = b"67890"
    assert "a" in cache and "b" in cache
    # Touch 'a' so 'b' becomes least-recently-used, then insert 'c'.
    assert cache["a"] == b"12345"
    cache["c"] = b"abcde"
    assert "a" in cache and "c" in cache and "b" not in cache
    # Entries larger than max_bytes are not retained.
    cache["huge"] = b"x" * 11
    assert "huge" not in cache


def test_legacy_partials_migrate_to_per_source_files_and_slim_cache(tmp_path, monkeypatch):
    from citypods.models import City
    from citypods.records import source_key

    output, args = _site(tmp_path, monkeypatch)
    city = City(
        slug="city-0",
        provider="faketest",
        source={"url": "https://example.test/0"},
        podcast_title="City 0",
        podcast_author="City",
        podcast_email="",
        podcast_description="Meetings",
    )
    monkeypatch.setattr("citypods.config.load_city_configs", lambda *_: [city])
    _seed_records(args, [city])

    # Build once to produce a valid partial record entry, then move it into legacy cache format.
    assert search.build_search_site(**args) == "complete"
    src_key = source_key(city)
    working = tmp_path / ".citypods-search-checkpoint/working"
    valid_partial = json.loads((working / f".search-partials/{src_key}.json").read_text())
    assert "u1" in valid_partial["records"]
    (working / f".search-partials/{src_key}.json").unlink()
    legacy_cache = {
        "shards": {},
        "partials": {
            src_key: valid_partial,
            "retired-source": {"version": search.SEARCH_CACHE_VERSION, "hash": "x", "records": {}},
        },
    }
    (working / ".search-cache.json").write_text(json.dumps(legacy_cache))

    monkeypatch.setattr(
        search,
        "_record_to_document",
        lambda *_a, **_kw: pytest.fail("should reuse migrated partial document"),
    )
    assert search.build_search_site(**args) == "complete"
    saved_cache = json.loads((working / ".search-cache.json").read_text())
    assert "partials" not in saved_cache
    migrated = json.loads((working / f".search-partials/{src_key}.json").read_text())
    assert migrated["records"]["u1"] == valid_partial["records"]["u1"]
    assert not (working / ".search-partials/retired-source.json").exists()


def test_sigterm_stops_search_gracefully_and_saves_checkpoint(tmp_path, monkeypatch):
    import os
    import signal
    from datetime import UTC, datetime

    from citypods.models import City, Episode
    from citypods.records import episode_to_record, save_records, source_key

    output, args = _site(tmp_path, monkeypatch)
    city = City(
        slug="city-0",
        provider="faketest",
        source={"url": "https://example.test/0"},
        podcast_title="City 0",
        podcast_author="City",
        podcast_email="",
        podcast_description="Meetings",
    )
    monkeypatch.setattr("citypods.config.load_city_configs", lambda *_: [city])
    src_key = source_key(city)
    save_records(
        args["state_dir"],
        src_key,
        {
            uid: episode_to_record(
                Episode(
                    guid=f"g-{uid}",
                    uid=uid,
                    title=f"Council {uid}",
                    published=datetime(2026, 1, 1, tzinfo=UTC),
                    video_url="https://example.test/video",
                )
            )
            for uid in ("u1", "u2")
        },
    )

    def send_sigterm(*_a, **_kw):
        os.kill(os.getpid(), signal.SIGTERM)
        return None

    monkeypatch.setattr(search, "_record_to_document", send_sigterm)
    result = search.build_search_site(**args)
    assert result.startswith("deferred")
    working = tmp_path / ".citypods-search-checkpoint/working"
    assert (working / ".search-cache.json").is_file()
    partial = json.loads((working / f".search-partials/{src_key}.json").read_text())
    assert len(partial["records"]) == 1


def test_search_workflow_build_step_has_timeout_below_job():
    workflow = yaml.safe_load(Path(".github/workflows/search.yml").read_text())
    build = workflow["jobs"]["build"]
    step = next(s for s in build["steps"] if s.get("name") == "Build bounded search")
    assert step["timeout-minutes"] < build["timeout-minutes"]
