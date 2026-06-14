"""Tests for the enrichment-stage pipeline."""

from __future__ import annotations

from datetime import UTC, datetime

from citypods.models import City, Episode
from citypods.stages import AudioStage, LinksStage, StageContext, default_stages, run_stages
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
        loudness_profile=None,
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


def test_run_stages_returns_stats_per_stage(tmp_path):
    eps = [_ep("g1")]
    stats = run_stages(FakeProvider(), _city(), eps, default_stages(), _ctx(tmp_path))
    expected = ["chapters", "timeline", "remap", "audio", "transcript", "links"]
    assert [s.name for s in stats] == expected
    # chapters is a no-op (FakeProvider has no fetch_chapters); audio hosts; links defaults.
    audio = next(s for s in stats if s.name == "audio")
    assert audio.ran == 1
    assert "audio" in audio.note()
    assert eps[0].links.get("canonical_video")


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
    assert eps[0].links["transcript"] == "https://t/x"
    # idempotent: episode already has chapters -> reused, no second fetch
    stats2 = ChaptersStage().process(p, _city(), eps, _ctx(tmp_path))
    assert stats2.reused == 1 and p.calls == 1


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
