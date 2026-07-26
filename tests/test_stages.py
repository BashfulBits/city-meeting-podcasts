"""Tests for the enrichment-stage pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

from citypods.models import City, Episode
from citypods.stages import (
    AudioStage,
    LinksStage,
    StageContext,
    StageStats,
    default_stages,
    enrich_stages,
    render_stages,
    run_stages,
)
from citypods.storage.local import LocalStorage


class FakeFfmpeg:
    def __init__(self):
        self.calls: list[str] = []

    def extract_audio(
        self,
        timeline,
        sources_by_id,
        dest,
        chapters=None,
        *,
        sources=None,
        loudness_profile=None,
        processing_profile=None,
        asset_resolver=None,
    ) -> None:
        first_url = next(iter(sources_by_id.values())) if sources_by_id else ""
        self.calls.append(first_url)
        dest.write_bytes(b"fake" * 2048)  # > #39 truncation-guard byte floor


class FakeProvider:
    def resolve_media_url(self, episode, source):
        return episode.video_url


def _city():
    return City(
        slug="x-tx",
        provider="civicplus",
        source={"feed_url": "x"},
        podcast_title="X",
        podcast_author="City of X",
        podcast_email="",
        podcast_description="d",
    )


def _ep(guid):
    return Episode(
        guid=guid,
        uid=f"uid-{guid}",
        title=f"M {guid}",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/x.m3u8",
        media_kind="hls",
        body="City Council",
    )


def _ctx(tmp_path, *, dry_run=False, storage=True, stop=None, chapters_per_source=10_000):
    return StageContext(
        storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn") if storage else None,
        ffmpeg=FakeFfmpeg(),
        max_kbps=96,
        dry_run=dry_run,
        stop=stop,
        chapters_per_source=chapters_per_source,
    )


def _stop_after(n):
    """A stop predicate that returns False for the first ``n`` calls, then True."""
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > n

    return stop


def test_default_stage_order_audio_affecting_before_audio():
    names = [s.name for s in default_stages()]
    # chapters change audio_spec_hash, so they must run before audio; links is feed-only after.
    assert names.index("chapters") < names.index("audio") < names.index("links")


def test_audio_stage_stops_when_signalled(tmp_path):
    eps = [_ep("g1"), _ep("g2"), _ep("g3")]
    ctx = _ctx(tmp_path, stop=_stop_after(2))  # encode two, then the run is superseded/over-window
    stats = AudioStage().process(FakeProvider(), _city(), eps, ctx)
    assert stats.ran == 2 and stats.skipped == 1
    assert sum(e.hosted_audio_url is not None for e in eps) == 2


def test_audio_stage_noop_in_dry_run(tmp_path):
    eps = [_ep("g1")]
    stats = AudioStage().process(FakeProvider(), _city(), eps, _ctx(tmp_path, dry_run=True))
    assert stats.ran == 0 and eps[0].hosted_audio_url is None


def test_audio_stage_noop_without_storage(tmp_path):
    eps = [_ep("g1")]
    stats = AudioStage().process(FakeProvider(), _city(), eps, _ctx(tmp_path, storage=False))
    assert stats.ran == 0 and eps[0].hosted_audio_url is None


def test_audio_stage_skips_withheld_availability(tmp_path):
    # H16 PR3: a confirmed-empty/withheld episode must not be encoded/hosted, while a playable
    # sibling in the same set is processed normally and its record block is left untouched.
    from citypods.availability import CONFIRMED_EMPTY, MediaAvailability

    good, withheld = _ep("good"), _ep("empty")
    withheld.media_availability = MediaAvailability(state=CONFIRMED_EMPTY)
    stats = AudioStage().process(FakeProvider(), _city(), [good, withheld], _ctx(tmp_path))
    assert stats.ran == 1
    assert good.hosted_audio_url is not None
    assert withheld.hosted_audio_url is None  # never hosted a bad enclosure


def test_run_stages_returns_stats_per_stage(tmp_path):
    eps = [_ep("g1")]
    stats = run_stages(FakeProvider(), _city(), eps, default_stages(), _ctx(tmp_path))
    expected = [
        "chapters",
        "timeline",
        "remap",
        "audio",
        "transcript",
        "links",
        "agenda_text",
        "minutes_text",
        "diarize",
        "tags",
    ]
    assert [s.name for s in stats] == expected
    # chapters is a no-op (FakeProvider has no fetch_chapters); audio hosts; links defaults.
    audio = next(s for s in stats if s.name == "audio")
    assert audio.ran == 1
    assert "audio" in audio.note()
    assert eps[0].links.get("canonical_video")


def test_completion_cache_skips_unchanged_episode_and_invalidates_on_input_change(tmp_path):
    calls = []

    class MarkerStage:
        name = "marker"
        version = "1"

        def process(self, provider, city, episodes, ctx):
            calls.append([ep.uid for ep in episodes])
            return StageStats(self.name, ran=len(episodes))

    episode = _ep("g1")
    stage = MarkerStage()
    run_stages(FakeProvider(), _city(), [episode], [stage], _ctx(tmp_path))
    run_stages(FakeProvider(), _city(), [episode], [stage], _ctx(tmp_path))
    assert calls == [["uid-g1"]]

    episode.video_url = "https://src/new.m3u8"
    run_stages(FakeProvider(), _city(), [episode], [stage], _ctx(tmp_path))
    assert calls == [["uid-g1"], ["uid-g1"]]


def test_completion_cache_handles_empty_results_new_episode_and_version_bumps(tmp_path):
    calls = []

    class EmptyStage:
        name = "chapters"
        version = "1"

        def process(self, provider, city, episodes, ctx):
            calls.append([ep.uid for ep in episodes])
            return StageStats(self.name, reused=len(episodes))

    stage = EmptyStage()
    first = _ep("g1")
    run_stages(FakeProvider(), _city(), [first], [stage], _ctx(tmp_path))
    assert first.stage_completion["chapters"]["state"] == "complete-empty"

    second = _ep("g2")
    run_stages(FakeProvider(), _city(), [first, second], [stage], _ctx(tmp_path))
    assert calls == [["uid-g1"], ["uid-g2"]]

    stage.version = "2"
    run_stages(FakeProvider(), _city(), [first, second], [stage], _ctx(tmp_path))
    assert calls[-1] == ["uid-g1", "uid-g2"]


def test_deferred_dirty_episode_does_not_poison_completed_sibling(tmp_path):
    calls = []

    class PartialStage:
        name = "marker"
        version = "1"
        should_skip = False

        def process(self, provider, city, episodes, ctx):
            calls.append([ep.uid for ep in episodes])
            return StageStats(
                self.name,
                skipped=len(episodes) if self.should_skip else 0,
                ran=0 if self.should_skip else len(episodes),
            )

    first, second = _ep("g1"), _ep("g2")
    stage = PartialStage()
    run_stages(FakeProvider(), _city(), [first, second], [stage], _ctx(tmp_path))
    second.video_url = "https://src/changed.m3u8"
    stage.should_skip = True
    run_stages(FakeProvider(), _city(), [first, second], [stage], _ctx(tmp_path))
    assert calls[-1] == ["uid-g2"]
    assert first.stage_completion["marker"]["state"] == "complete"


def test_legacy_artifact_inference_skips_stage_without_marker(tmp_path):
    calls = []

    class LegacyAudioStage:
        name = "audio"
        version = "1"

        def process(self, provider, city, episodes, ctx):
            calls.append(episodes)
            return StageStats(self.name, ran=len(episodes))

    episode = _ep("g1")
    episode.hosted_audio_url = "https://cdn/g1.m4a"
    run_stages(FakeProvider(), _city(), [episode], [LegacyAudioStage()], _ctx(tmp_path))
    assert calls == []


def test_run_stages_halts_after_provider_throttle(tmp_path):
    calls: list[str] = []

    class _ThrottleStage:
        name = "timeline"
        version = "1"

        def process(self, provider, city, episodes, ctx):
            calls.append(self.name)
            return StageStats(self.name, rate_limited=1, errors=["HTTP 403"])

    class _MustNotRun:
        name = "audio"
        version = "1"

        def process(self, provider, city, episodes, ctx):
            calls.append(self.name)
            return StageStats(self.name)

    stats = run_stages(
        FakeProvider(), _city(), [_ep("g1")], [_ThrottleStage(), _MustNotRun()], _ctx(tmp_path)
    )

    assert calls == ["timeline"]
    assert [s.name for s in stats] == ["timeline"]


def test_transcribe_lane_skips_the_audio_chain(tmp_path):
    # H6b lane isolation (review/12 §H6): the ASR lane must NOT run AudioStage, or it would write an
    # audio block from its start-of-run snapshot and (via the whole-record push) clobber the audio a
    # concurrent audio run just hosted. It runs only its own work-class stage.
    import dataclasses

    eps = [_ep("g1")]
    ctx = dataclasses.replace(_ctx(tmp_path), lane="transcribe")
    stats = run_stages(FakeProvider(), _city(), eps, default_stages(), ctx)
    assert [s.name for s in stats] == ["transcript"]
    assert eps[0].hosted_audio_url is None  # audio was not (re-)materialized in the ASR lane


def test_audio_lane_skips_transcript(tmp_path):
    import dataclasses

    eps = [_ep("g1")]
    ctx = dataclasses.replace(_ctx(tmp_path), lane="audio")
    stats = run_stages(FakeProvider(), _city(), eps, default_stages(), ctx)
    names = [s.name for s in stats]
    assert "audio" in names and "chapters" in names  # the audio work-class chain runs
    assert "transcript" not in names  # but not transcription


def test_default_lane_none_runs_every_stage(tmp_path):
    # A full enrich / manual single-source run (lane=None) owns everything → no stage is skipped.
    eps = [_ep("g1")]
    stats = run_stages(FakeProvider(), _city(), eps, default_stages(), _ctx(tmp_path))
    assert [s.name for s in stats] == [
        "chapters",
        "timeline",
        "remap",
        "audio",
        "transcript",
        "links",
        "agenda_text",
        "minutes_text",
        "diarize",
        "tags",
    ]


def test_production_stage_composition_extracts_minutes_before_diarization():
    assert [stage.name for stage in render_stages()] == ["links"]
    names = [stage.name for stage in enrich_stages()]
    assert names.index("links") < names.index("agenda_text") < names.index("minutes_text")
    assert names.index("minutes_text") < names.index("diarize")
    assert names.index("diarize") < names.index("tags")


def test_links_stage_defaults_canonical_video_and_is_idempotent(tmp_path):
    eps = [_ep("g1")]
    s1 = LinksStage().process(FakeProvider(), _city(), eps, _ctx(tmp_path))
    assert s1.ran == 1 and s1.reused == 0
    assert eps[0].links == {"canonical_video": "https://src/x.m3u8"}
    # Re-running with the link already present is a no-op (reused, not re-written).
    s2 = LinksStage().process(FakeProvider(), _city(), eps, _ctx(tmp_path))
    assert s2.ran == 0 and s2.reused == 1


def test_links_stage_merges_provider_supplied_links(tmp_path):
    class P(FakeProvider):
        def episode_links(self, ep, source):
            return {"agenda": "https://docs/agenda.pdf", "minutes": ""}

    eps = [_ep("g1")]
    eps[0].links = {"canonical_video": "https://watch/page"}
    LinksStage().process(P(), _city(), eps, _ctx(tmp_path))
    # provider agenda merged, empty minutes dropped, existing canonical_video preserved
    assert eps[0].links == {
        "canonical_video": "https://watch/page",
        "agenda": "https://docs/agenda.pdf",
    }


def test_links_stage_adds_meetings_url_on_every_episode(tmp_path):
    city = _city()
    city.meetings_url = "https://x.tx.gov/agendas"
    eps = [_ep("g1"), _ep("g2")]
    LinksStage().process(FakeProvider(), city, eps, _ctx(tmp_path))
    assert all(e.links.get("meetings") == "https://x.tx.gov/agendas" for e in eps)


def test_links_stage_meetings_falls_back_to_city_website(tmp_path):
    city = _city()
    city.city_website = "https://x.tx.gov"
    eps = [_ep("g1")]
    LinksStage().process(FakeProvider(), city, eps, _ctx(tmp_path))
    assert eps[0].links.get("meetings") == "https://x.tx.gov"


def test_links_stage_no_meetings_link_when_neither_configured(tmp_path):
    eps = [_ep("g1")]
    LinksStage().process(FakeProvider(), _city(), eps, _ctx(tmp_path))
    assert "meetings" not in eps[0].links


class ChapterProvider(FakeProvider):
    def __init__(self, chapters, transcript=None):
        self._chapters = chapters
        self._transcript = transcript
        self.calls = 0

    def fetch_chapters(self, episode, source):
        self.calls += 1
        return self._chapters, self._transcript


def test_chapters_stage_sets_chapters_and_transcript_link(tmp_path):
    from citypods.stages import ChaptersStage

    eps = [_ep("g1")]
    p = ChapterProvider([{"start": 5, "end": 60, "title": "Item"}], transcript="https://t/x")
    stats = ChaptersStage().process(p, _city(), eps, _ctx(tmp_path))
    assert stats.ran == 1 and eps[0].chapters[0]["title"] == "Item"
    assert eps[0].source_chapters == [{"start": 5, "end": 60, "title": "Item"}]
    assert eps[0].links["transcript"] == "https://t/x"
    # idempotent: episode already has chapters -> reused, no second fetch
    stats2 = ChaptersStage().process(p, _city(), eps, _ctx(tmp_path))
    assert stats2.reused == 1 and p.calls == 1


def test_chapters_stage_backfills_source_chapters_from_existing_source_basis(tmp_path):
    from citypods.stages import ChaptersStage

    eps = [_ep("g1")]
    eps[0].chapters = [{"start": 5, "title": "Item"}]
    p = ChapterProvider([{"start": 10, "title": "ignored"}])
    stats = ChaptersStage().process(p, _city(), eps, _ctx(tmp_path))
    assert stats.reused == 1
    assert p.calls == 0
    assert eps[0].source_chapters == [{"start": 5, "title": "Item"}]


def test_chapters_stage_refetches_old_served_only_records_to_backfill_source_chapters(tmp_path):
    from citypods.stages import ChaptersStage

    eps = [_ep("g1")]
    eps[0].chapters = [{"start": 300, "title": "Stale served"}]
    eps[0].chapters_basis = "served:older-version"
    p = ChapterProvider([{"start": 5, "title": "Fresh source"}])
    stats = ChaptersStage().process(p, _city(), eps, _ctx(tmp_path))
    assert stats.ran == 1
    assert p.calls == 1
    assert eps[0].source_chapters == [{"start": 5, "title": "Fresh source"}]
    assert eps[0].chapters == [{"start": 5, "title": "Fresh source"}]
    assert eps[0].chapters_basis == "source:s0"


def test_chapters_stage_skips_multisource_served_only_records(tmp_path):
    from citypods.stages import ChaptersStage
    from citypods.timeline import SourceMedia

    eps = [_ep("g1")]
    eps[0].chapters = [{"start": 300, "title": "Concat served"}]
    eps[0].chapters_basis = "served:concat-v1"
    eps[0].sources = [
        SourceMedia(
            id="s0", provider="swagit", ref="u0", media_kind="direct", duration=1.0, watch_url=None
        ),
        SourceMedia(
            id="s1", provider="swagit", ref="u1", media_kind="direct", duration=1.0, watch_url=None
        ),
    ]
    p = ChapterProvider([{"start": 5, "title": "Fresh source"}])
    stats = ChaptersStage().process(p, _city(), eps, _ctx(tmp_path))
    assert stats.reused == 1
    assert p.calls == 0
    assert eps[0].source_chapters == []


def test_chapters_stage_stops_when_signalled(tmp_path):
    from citypods.stages import ChaptersStage

    eps = [_ep("a"), _ep("b")]
    ctx = _ctx(tmp_path, stop=_stop_after(1))  # fetch one, then yield
    p = ChapterProvider([{"start": 0, "end": 1, "title": "x"}])
    stats = ChaptersStage().process(p, _city(), eps, ctx)
    assert stats.ran == 1 and stats.skipped == 1  # second episode deferred to a later run


def test_chapters_stage_caps_per_source(tmp_path):
    """Chapters are cheap+numerous, so a per-source count bounds them (unlike audio's wall-clock):
    only chapters_per_source pages are scraped per run; the rest defer."""
    from citypods.stages import ChaptersStage

    eps = [_ep("a"), _ep("b"), _ep("c")]
    ctx = _ctx(tmp_path, chapters_per_source=2)
    p = ChapterProvider([{"start": 0, "end": 1, "title": "x"}])
    stats = ChaptersStage().process(p, _city(), eps, ctx)
    assert stats.ran == 2 and stats.skipped == 1 and p.calls == 2


def test_chapters_stage_noop_without_provider_support(tmp_path):
    from citypods.stages import ChaptersStage

    eps = [_ep("g1")]
    stats = ChaptersStage().process(FakeProvider(), _city(), eps, _ctx(tmp_path))
    assert stats.ran == 0 and not eps[0].chapters


def test_chapters_stage_does_not_clobber_existing_transcript(tmp_path):
    from citypods.stages import ChaptersStage

    eps = [_ep("g1")]
    eps[0].links = {"transcript": "https://pdf/better"}  # richer link already set
    p = ChapterProvider([{"start": 1, "title": "x"}], transcript="https://srt/worse")
    ChaptersStage().process(p, _city(), eps, _ctx(tmp_path))
    assert eps[0].links["transcript"] == "https://pdf/better"  # preserved


def test_llm_disabled_run_invalidates_stale_candidates_on_taxonomy_change(tmp_path):
    """When ctx.tag_backend is None (dry run, misconfigured LLM_MODEL, ...) TagsStage used to
    republish ep.llm_tag_candidates unconditionally -- it must instead validate them against a
    recipe recomputed from their OWN recorded route/prompt_version (there's no live backend this
    run to compute a current one), so a taxonomy/transcript/agenda change still invalidates a
    stale candidate even with no LLM call happening this run."""
    from citypods.stages import TagsStage
    from citypods.tags import TAG_PROMPT_VERSION, tag_recipe_hash, taxonomy_from_dict

    old_taxonomy = taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"x": "https://example.test"},
            "tags": [{"id": "housing", "source_refs": ["x"], "rules": {"include": ["housing"]}}],
        }
    )
    taxonomy_path = tmp_path / "taxonomy.yml"
    taxonomy_path.write_text(
        "version: 2\n"
        "source_refs: {x: 'https://example.test'}\n"
        "tags:\n"
        "  - id: housing\n"
        "    source_refs: [x]\n"
        "    rules: {include: [housing]}\n"
    )

    ep = _ep("g1")
    recorded_recipe = tag_recipe_hash(
        old_taxonomy,
        agenda_item_titles="",
        agenda_text="",
        transcript_text="",
        llm_enabled=True,
        chapter_inputs=[],
        llm_route="litellm:gemini/gemini-3-flash-preview",
        prompt_version=TAG_PROMPT_VERSION,
    )
    ep.llm_tag_candidates = [
        {
            "id": "housing",
            "source": "llm",
            "confidence": 0.9,
            "feature": "topic-tags",
            "provider_model": "litellm:gemini/gemini-3-flash-preview",
            "prompt_version": TAG_PROMPT_VERSION,
            "taxonomy_version": 1,
            "scope": "episode",
            "evidence": [{"where": "agenda", "quote": "housing"}],
        }
    ]
    ep.tags_llm_recipe_hash = recorded_recipe
    ep.tags_spec_hash = "stale-marker-that-never-matches-a-real-hash"

    ctx = _ctx(tmp_path)
    ctx.taxonomy_path = taxonomy_path
    ctx.tag_backend = None  # llm disabled/unavailable this run

    TagsStage().process(None, _city(), [ep], ctx)

    assert [tag["id"] for tag in ep.tags] == []
    assert ep.llm_tag_candidates == []


def test_tags_stage_skips_storage_on_unchanged_episode(tmp_path):
    """A tag-lane run that finds nothing changed must not re-fetch/re-hash agenda+transcript
    text just to re-derive "nothing changed" -- that per-episode storage round trip, paid on
    every run for the *entire* backlog regardless of whether anything actually changed, is what
    stalled a real scheduled tag run for ~10 minutes without making an LLM call (investigated on
    the BashfulBits/city-meeting-podcasts `tag` workflow). ``tag_input_fingerprint`` lets an
    unchanged episode short-circuit before any storage access at all."""
    from citypods.stages import TagsStage

    ep = _ep("g1")
    ep.links = {"agenda_text_artifact_key": "documents/x-tx/uid-g1/agenda_text-abc123"}
    ep.transcript_key = "transcripts/x-tx/uid-g1-asr-deadbeef.txt"
    ep.transcript_format = "txt"

    ctx = _ctx(tmp_path)
    ctx.storage.put_file(
        ep.links["agenda_text_artifact_key"],
        _write_temp(tmp_path, "agenda.txt", b"Agenda item: housing plan"),
        "text/plain",
    )
    ctx.storage.put_file(
        ep.transcript_key,
        _write_temp(tmp_path, "transcript.txt", b"discussion of the housing plan"),
        "text/plain",
    )
    ctx.tag_backend = None  # rules-only: no live LLM needed to reach a terminal, cacheable state

    first = TagsStage().process(None, _city(), [ep], ctx)
    assert first.ran == 1
    assert ep.tags_input_fingerprint is not None
    assert ep.tags_spec_hash is not None

    class _CountingStorage:
        """Wraps the same backing store but fails the test if TagsStage still reads through it."""

        def __init__(self, inner):
            self._inner = inner
            self.reads = 0

        def exists(self, key):
            self.reads += 1
            return self._inner.exists(key)

        def get_file(self, key, local_path):
            self.reads += 1
            return self._inner.get_file(key, local_path)

    counting = _CountingStorage(ctx.storage)
    ctx.storage = counting

    second = TagsStage().process(None, _city(), [ep], ctx)
    assert second.reused == 1
    assert second.ran == 0
    assert counting.reads == 0


def test_tags_stage_backfills_fingerprint_for_pre_existing_resolved_episode(tmp_path):
    """An episode fully resolved by TagsStage *before* ``tags_input_fingerprint`` existed has
    ``tags_spec_hash`` set but ``tags_input_fingerprint`` is (and forever stays) ``None`` --
    exactly the state of the entire backlog the first time this field shipped. That episode can
    never hit the cheap pre-check (it requires ``tags_input_fingerprint is not None``), so it
    falls through to the storage-fetch path, discovers nothing changed via the
    ``tags_spec_hash == projection_hash`` short-circuit, and used to ``continue`` right there
    without ever recording the fingerprint -- silently repeating the full storage-fetch-and-hash
    cost for the entire legacy backlog on *every* run forever, not just once. This is what kept
    the scheduled `tag` workflow timing out even after the pre-check was added. The mid-tier
    short-circuit must backfill the fingerprint before continuing so only THIS run pays the
    storage cost and every run after it uses the cheap pre-check."""
    from citypods.stages import TagsStage

    ep = _ep("g1")
    ep.links = {"agenda_text_artifact_key": "documents/x-tx/uid-g1/agenda_text-abc123"}
    ep.transcript_key = "transcripts/x-tx/uid-g1-asr-deadbeef.txt"
    ep.transcript_format = "txt"

    ctx = _ctx(tmp_path)
    ctx.storage.put_file(
        ep.links["agenda_text_artifact_key"],
        _write_temp(tmp_path, "agenda.txt", b"Agenda item: housing plan"),
        "text/plain",
    )
    ctx.storage.put_file(
        ep.transcript_key,
        _write_temp(tmp_path, "transcript.txt", b"discussion of the housing plan"),
        "text/plain",
    )
    ctx.tag_backend = None

    # Simulate a pre-existing resolved episode: run once to get a real terminal tags_spec_hash,
    # then wipe the fingerprint back to None as if this episode predates the field entirely.
    TagsStage().process(None, _city(), [ep], ctx)
    assert ep.tags_spec_hash is not None
    ep.tags_input_fingerprint = None

    backfill_run = TagsStage().process(None, _city(), [ep], ctx)
    assert backfill_run.reused == 0  # not the cheap pre-check -- this run still pays storage
    assert backfill_run.ran == 1  # backfilled the fingerprint, so counted as a mutation
    assert ep.tags_input_fingerprint is not None

    class _CountingStorage:
        def __init__(self, inner):
            self._inner = inner
            self.reads = 0

        def exists(self, key):
            self.reads += 1
            return self._inner.exists(key)

        def get_file(self, key, local_path):
            self.reads += 1
            return self._inner.get_file(key, local_path)

    counting = _CountingStorage(ctx.storage)
    ctx.storage = counting

    steady_state_run = TagsStage().process(None, _city(), [ep], ctx)
    assert steady_state_run.reused == 1
    assert steady_state_run.ran == 0
    assert counting.reads == 0


def test_tags_stage_defers_without_storage_fetch_once_budget_spent(tmp_path):
    """Once the wall-clock ``stop()`` window is spent, an unresolved episode must be deferred
    *before* the agenda/transcript storage fetch, not after. The only other stop() check sits past
    those two per-episode round trips, so without this gate a spent budget still let the pass grind
    through the whole backlog's fetches until GitHub's hard job timeout cancelled the run mid-pass
    (~8 minutes of it after the graceful stop had already fired). A deferred episode is untouched,
    so it retries next run."""
    from citypods.stages import TagsStage

    ep = _ep("g1")
    ep.links = {"agenda_text_artifact_key": "documents/x-tx/uid-g1/agenda_text-abc123"}
    ep.transcript_key = "transcripts/x-tx/uid-g1-asr-deadbeef.txt"
    ep.transcript_format = "txt"
    # Unresolved: no cached fingerprint, so the cheap pre-check can't short-circuit it — exactly
    # the backlog episodes that were being re-fetched every run.
    assert ep.tags_input_fingerprint is None

    class _CountingStorage:
        def __init__(self, inner):
            self._inner = inner
            self.reads = 0

        def exists(self, key):
            self.reads += 1
            return self._inner.exists(key)

        def get_file(self, key, local_path):
            self.reads += 1
            return self._inner.get_file(key, local_path)

    ctx = _ctx(tmp_path, stop=_stop_after(0))  # stop() True from the very first check
    ctx.storage = _CountingStorage(ctx.storage)

    stats = TagsStage().process(None, _city(), [ep], ctx)

    # Deferred (skipped, grouped under the budget-stop reason) with no storage access and no
    # mutation to the episode's tag state.
    assert stats.defer_reasons.get("tag-budget-stop") == 1
    assert stats.skipped == 1
    assert stats.ran == 0
    assert ctx.storage.reads == 0
    assert ep.tags == [] and ep.tags_spec_hash is None and ep.tags_input_fingerprint is None


class _FakeTagBackend:
    """A stand-in LLM tag backend: real enough for TagsStage to treat the run as LLM-enabled and
    to compute a matching `llm_route`, without pulling in the live provider machinery. Its dispatch
    path is never reached by the triage tests below (they short-circuit before any backend call)."""

    name = "litellm"

    class config:  # noqa: N801 — mirrors the real backend's `.config.model` attribute shape
        model = "gemini/gemini-3.1-flash-lite"

    storage = None


def _mark_pending(ep, ctx, taxonomy):
    """Put ``ep`` into the 'rules tagged, LLM still pending, inputs unchanged' state a real run
    leaves behind: a cached input fingerprint that matches the current inputs, a set spec hash, and
    no resolved LLM recipe hash. Computed with the exact inputs TagsStage.process derives so the
    triage's cheap pre-check recognises the episode as unchanged."""
    from citypods.llm_evaluation import config_from_mapping, policy_fingerprint
    from citypods.tags import TAG_PROMPT_VERSION, tag_input_fingerprint

    route = f"{ctx.tag_backend.name}:{ctx.tag_backend.config.model}"
    admission = policy_fingerprint(
        config_from_mapping(ctx.llm_evaluation_config),
        {"version": 1, "reviews": {}, "matrix": [], "trend": []},
    )
    ep.tags_input_fingerprint = tag_input_fingerprint(
        ep,
        taxonomy,
        llm_enabled=True,
        llm_route=route,
        prompt_version=TAG_PROMPT_VERSION,
        admission_policy=admission,
    )
    ep.tags_spec_hash = "rules-only-hash"  # a real rules_hash; not equal to projection_hash
    ep.tags_llm_recipe_hash = None  # LLM tag never resolved -> still "pending"


class _CountingStorage:
    def __init__(self, inner):
        self._inner = inner
        self.reads = 0

    def exists(self, key):
        self.reads += 1
        return self._inner.exists(key)

    def get_file(self, key, local_path):
        self.reads += 1
        return self._inner.get_file(key, local_path)


def test_tags_stage_pending_episode_skips_fetch_when_llm_quota_exhausted(tmp_path):
    """The core no-re-fetch behavior. A backlog episode whose rules tags are already cached and
    that only awaits a quota-limited LLM tag ('pending') must NOT re-fetch its agenda/transcript
    text once the backend has reported it is out of dispatch capacity for the run. It defers in
    memory, untouched, and is retried on a later run once quota frees -- instead of paying two
    storage round trips on every run just to re-discover 'still pending' (what made the whole
    backlog re-fetch and the job time out)."""
    from citypods.stages import TagsStage
    from citypods.tags import load_taxonomy

    ep = _ep("g1")
    ep.links = {"agenda_text_artifact_key": "documents/x-tx/uid-g1/agenda_text-abc123"}
    ep.transcript_key = "transcripts/x-tx/uid-g1-asr-deadbeef.txt"
    ep.transcript_format = "txt"

    ctx = _ctx(tmp_path)
    ctx.tag_backend = _FakeTagBackend()
    _mark_pending(ep, ctx, load_taxonomy(ctx.taxonomy_path))
    ctx.tag_llm_dispatch_exhausted.set()  # backend already out of capacity this run
    ctx.storage = _CountingStorage(ctx.storage)

    stats = TagsStage().process(None, _city(), [ep], ctx)

    assert stats.defer_reasons.get("tag-llm-no-quota") == 1
    assert stats.skipped == 1 and stats.ran == 0
    assert ctx.storage.reads == 0  # the whole point: no heavy fetch


def test_tags_stage_loads_taxonomy_and_eval_state_once_per_run(tmp_path, monkeypatch):
    """A real scheduled tag run showed only 2 live LLM calls across a 13k-episode backlog before
    exhausting its wall-clock budget. Root cause: TagsStage.process() is invoked once PER EPISODE
    by the global queue, and it re-read + re-parsed the taxonomy YAML and calibration-state JSON
    from local disk on every single call -- ~28ms/call for a real ~17KB taxonomy alone, ~6 minutes
    of pure YAML parsing across the backlog before dispatch logic ever ran. Both are read-only for
    the whole run, so they must load at most once per `ctx` (the same object across every
    per-episode call in one build), cached on `ctx.tag_taxonomy_cache`."""
    import json

    import citypods.llm_evaluation as eval_mod
    import citypods.tags as tags_mod
    from citypods.stages import TagsStage

    load_taxonomy_calls = {"n": 0}
    real_load_taxonomy = tags_mod.load_taxonomy

    def _counting_load_taxonomy(*a, **k):
        load_taxonomy_calls["n"] += 1
        return real_load_taxonomy(*a, **k)

    monkeypatch.setattr(tags_mod, "load_taxonomy", _counting_load_taxonomy)

    load_state_calls = {"n": 0}
    real_load_state = eval_mod.load_state

    def _counting_load_state(*a, **k):
        load_state_calls["n"] += 1
        return real_load_state(*a, **k)

    monkeypatch.setattr(eval_mod, "load_state", _counting_load_state)

    eval_state_path = tmp_path / "llm_evaluation.json"
    eval_state_path.write_text(json.dumps({"version": 1, "reviews": {}, "matrix": [], "trend": []}))

    ctx = _ctx(tmp_path)
    ctx.llm_evaluation_state_path = eval_state_path
    stage = TagsStage()  # one instance, one ctx -- mirrors the global queue reusing both

    eps = [_ep("g1"), _ep("g2"), _ep("g3")]
    for ep in eps:
        stage.process(None, _city(), [ep], ctx)

    assert load_taxonomy_calls["n"] == 1, f"loaded taxonomy {load_taxonomy_calls['n']}x, want 1"
    assert load_state_calls["n"] == 1, f"loaded eval state {load_state_calls['n']}x, want 1"

    # A fresh ctx (a different run/build) must NOT share the cache -- the cache lives on ctx, and
    # ctx is constructed fresh per build, so this is the natural boundary, not a special case to
    # maintain by hand.
    ctx2 = _ctx(tmp_path)
    ctx2.llm_evaluation_state_path = eval_state_path
    stage.process(None, _city(), [_ep("g4")], ctx2)
    assert load_taxonomy_calls["n"] == 2
    assert load_state_calls["n"] == 2


def test_tags_stage_caches_malformed_evaluation_config_as_eval_error(tmp_path):
    """`config_from_mapping()` and `policy_fingerprint()` run inside the same cache-populate block
    as `load_state()`, so a malformed `tagging.evaluation` config (e.g. a non-numeric
    `minimum_reviews`) must degrade the same way a corrupt state file already does: cached once as
    `eval_error` and reported cheaply on every subsequent call, not re-raised uncaught out of every
    one of this run's per-episode process() calls."""
    from citypods.stages import TagsStage

    ctx = _ctx(tmp_path)
    ctx.llm_evaluation_config = {"minimum_reviews": "not-a-number"}

    stats = TagsStage().process(None, _city(), [_ep("g1")], ctx)

    assert "eval_error" in ctx.tag_taxonomy_cache
    assert stats.errors and "LLM evaluation state unavailable" in stats.errors[0]

    # Cached, not re-raised: a second call with the same broken ctx reports the same error again
    # instead of blowing up.
    stats2 = TagsStage().process(None, _city(), [_ep("g2")], ctx)
    assert stats2.errors and "LLM evaluation state unavailable" in stats2.errors[0]


def test_tags_stage_cache_population_is_atomic_under_concurrent_workers(tmp_path, monkeypatch):
    """The global queue calls TagsStage.process() from a worker thread pool, all sharing one
    `ctx`. The cache bundle (taxonomy, evaluation_config, evaluation_state, admission_policy) is
    written as three separate, non-atomic dict assignments -- without a lock, one thread could
    write `evaluation_state` and then be preempted before writing `admission_policy`; a second
    thread arriving in that window would see `evaluation_state` already cached, skip
    initialization entirely, and KeyError reading `cache["admission_policy"]`. An injected delay
    between those two writes widens that window so the race would be near-certain to reproduce
    without `tag_taxonomy_cache_lock` serializing the whole check-then-populate sequence."""
    import json
    import threading
    import time

    import citypods.llm_evaluation as eval_mod
    import citypods.tags as tags_mod
    from citypods.stages import TagsStage

    load_taxonomy_calls = {"n": 0}
    real_load_taxonomy = tags_mod.load_taxonomy

    def _slow_load_taxonomy(*a, **k):
        load_taxonomy_calls["n"] += 1
        result = real_load_taxonomy(*a, **k)
        time.sleep(0.02)  # widen the window between the taxonomy and eval-state cache writes
        return result

    monkeypatch.setattr(tags_mod, "load_taxonomy", _slow_load_taxonomy)

    load_state_calls = {"n": 0}
    real_load_state = eval_mod.load_state

    def _slow_load_state(*a, **k):
        load_state_calls["n"] += 1
        result = real_load_state(*a, **k)
        time.sleep(0.02)  # widen the window between the eval-state and admission-policy writes
        return result

    monkeypatch.setattr(eval_mod, "load_state", _slow_load_state)

    eval_state_path = tmp_path / "llm_evaluation.json"
    eval_state_path.write_text(json.dumps({"version": 1, "reviews": {}, "matrix": [], "trend": []}))

    ctx = _ctx(tmp_path)
    ctx.llm_evaluation_state_path = eval_state_path
    stage = TagsStage()

    n_threads = 12
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    barrier = threading.Barrier(n_threads)

    def _worker(i):
        barrier.wait()  # line every thread up to hit process() at the same instant
        try:
            stage.process(None, _city(), [_ep(f"g{i}")], ctx)
        except BaseException as exc:  # noqa: BLE001 -- capture from a worker thread to re-raise
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"worker thread(s) raised: {errors}"
    assert load_taxonomy_calls["n"] == 1, f"loaded taxonomy {load_taxonomy_calls['n']}x, want 1"
    assert load_state_calls["n"] == 1, f"loaded eval state {load_state_calls['n']}x, want 1"


def test_tags_stage_pending_episode_still_attempts_when_quota_available(tmp_path):
    """The flip side: while the backend still has capacity (exhaustion flag unset), a pending
    episode must NOT be skipped -- it proceeds to fetch and re-attempt the LLM. Proven by the
    storage read: the triage did not short-circuit at the no-quota gate."""
    from citypods.stages import TagsStage
    from citypods.tags import load_taxonomy

    ep = _ep("g1")
    ep.links = {"agenda_text_artifact_key": "documents/x-tx/uid-g1/agenda_text-abc123"}
    ep.transcript_key = "transcripts/x-tx/uid-g1-asr-deadbeef.txt"
    ep.transcript_format = "txt"

    ctx = _ctx(tmp_path)
    ctx.storage.put_file(
        ep.links["agenda_text_artifact_key"],
        _write_temp(tmp_path, "agenda.txt", b"Agenda item: housing plan"),
        "text/plain",
    )
    ctx.storage.put_file(
        ep.transcript_key,
        _write_temp(tmp_path, "transcript.txt", b"discussion of the housing plan"),
        "text/plain",
    )
    ctx.tag_backend = _FakeTagBackend()
    _mark_pending(ep, ctx, load_taxonomy(ctx.taxonomy_path))
    assert not ctx.tag_llm_dispatch_exhausted.is_set()  # capacity available
    ctx.storage = _CountingStorage(ctx.storage)

    # The real dispatch machinery (pydantic/instructor) isn't installed here, so the LLM attempt
    # raises and is caught as an item-local error -- but only AFTER the fetch, which is what we
    # assert: the episode was NOT skipped by the no-quota gate.
    TagsStage().process(None, _city(), [ep], ctx)
    assert ctx.storage.reads > 0


def test_tag_dispatch_sets_exhausted_flag_only_for_a_fresh_attempt(tmp_path, monkeypatch):
    """A ``dispatched=True`` result can come from ``LiteLLMBackend.run_inference``'s own cache
    short-circuit -- an existing, still-pending deferred-registry entry for this exact recipe,
    meaning only that the daily deferred sweep hasn't reconciled it yet -- rather than a fresh,
    live pacing attempt discovering no quota is available right now. Only the fresh-attempt case
    may set ``ctx.tag_llm_dispatch_exhausted``: a cache hit says nothing about current capacity,
    and treating it as exhaustion would prematurely skip-with-no-fetch the rest of the run's
    backlog even while real quota is sitting unused."""
    import citypods.compute.llm_deferred as llm_deferred_mod
    import citypods.tags as tags_mod
    from citypods.compute.base import InferenceJob, JobHandle
    from citypods.stages import TagsStage
    from tests._cas_fake import MemStorage

    # llm_tag_suggestions() registers the real Instructor/Pydantic response contract before ever
    # reaching the backend call; irrelevant to the JobHandle branch under test here (the contract
    # is only consumed on a resolved result, not a deferred one), so stub it to isolate this test
    # from that unrelated dependency.
    monkeypatch.setattr(tags_mod, "ensure_llm_contract", lambda: None)

    class _DispatchingBackend:
        """Always returns a deferred JobHandle -- stands in for either a genuinely quota-exhausted
        live attempt or a cache short-circuit; TagsStage must tell them apart via the pre-dispatch
        peek, not via this return value alone."""

        name = "litellm"

        class config:
            model = "gemini/gemini-3.1-flash-lite"

        def __init__(self, storage):
            self.storage = storage

        def run_inference(self, job: InferenceJob):
            return JobHandle(
                task=job.task,
                recipe_hash=job.recipe_hash,
                backend="litellm",
                ref=f"deferred:{job.recipe_hash}",
            )

    def _episode_with_content(guid):
        ep = _ep(guid)
        ep.links = {"agenda_text_artifact_key": f"documents/x-tx/uid-{guid}/agenda_text-abc"}
        ep.transcript_key = f"transcripts/x-tx/uid-{guid}-asr-deadbeef.txt"
        ep.transcript_format = "txt"
        return ep

    def _ctx_with_backend(guid):
        ctx = _ctx(tmp_path)
        ctx.storage.put_file(
            f"documents/x-tx/uid-{guid}/agenda_text-abc",
            _write_temp(tmp_path, f"agenda-{guid}.txt", b"Agenda item: housing plan"),
            "text/plain",
        )
        ctx.storage.put_file(
            f"transcripts/x-tx/uid-{guid}-asr-deadbeef.txt",
            _write_temp(tmp_path, f"transcript-{guid}.txt", b"discussion of the housing plan"),
            "text/plain",
        )
        ctx.tag_backend = _DispatchingBackend(MemStorage())
        return ctx

    # Case 1: no pre-existing registry entry -> this dispatch is a genuinely fresh, live attempt.
    ctx_fresh = _ctx_with_backend("fresh")
    TagsStage().process(None, _city(), [_episode_with_content("fresh")], ctx_fresh)
    assert ctx_fresh.tag_llm_dispatch_exhausted.is_set()

    # Case 2: look_up_deferred reports an existing pending entry for whatever recipe is queried --
    # simulating run_inference's own cache short-circuit having already fired inside
    # llm_tag_suggestions -- so this dispatch is a stale poll, not a live quota check.
    def _always_pending(storage, recipe_hash):
        return JobHandle(task="tag", recipe_hash=recipe_hash, backend="litellm", ref="deferred:x")

    monkeypatch.setattr(llm_deferred_mod, "look_up_deferred", _always_pending)
    ctx_cached = _ctx_with_backend("cached")
    TagsStage().process(None, _city(), [_episode_with_content("cached")], ctx_cached)
    assert not ctx_cached.tag_llm_dispatch_exhausted.is_set()


def _write_temp(tmp_path, name, content: bytes):
    path = tmp_path / "_uploads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
