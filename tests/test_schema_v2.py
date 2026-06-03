"""Tests for INFRA-2 (issue #143): schema v2, generalized audio_spec_hash, rebuild CLI.

Acceptance criteria:
- Identity episodes hash byte-identically to v1 (no re-encode storm).
- Round-trip tests: episode → record → episode preserves v2 fields.
- Duration-semantics migration test.
- Setting audio_rebuild re-keys ONLY the stamped records.
- Date-range select stamps only records within the range (inclusive; open-ended honored).
- --drop-object clears the audio pointer (same key re-encodes).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from citypods.cli import main as cli_main
from citypods.models import City, Episode
from citypods.records import (
    audio_spec_hash,
    episode_to_record,
    load_records,
    record_to_episode,
    save_records,
    source_key,
)
from citypods.timeline import Segment, SourceMedia, Timeline, identity_timeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _city(slug="test-tx"):
    return City(
        slug=slug,
        provider="granicus",
        source={"feed_url": "https://example.granicus.com/feed"},
        podcast_title="Test",
        podcast_author="City of Test",
        podcast_email="",
        podcast_description="d",
    )


def _ep(guid="g1", url="https://example.granicus.com/clip/1.mp4"):
    return Episode(
        guid=guid,
        uid=f"uid-{guid}",
        title="Council Meeting",
        published=datetime(2026, 5, 19, 16, 0, tzinfo=UTC),
        video_url=url,
        duration=3600,
        chapters=[{"start": 0, "title": "Call to order"}],
    )


def _src(id="s0") -> SourceMedia:
    return SourceMedia(
        id=id,
        provider="granicus",
        ref="https://example.granicus.com/clip/1.mp4",
        media_kind="direct",
        duration=3600.0,
        watch_url="https://example.granicus.com/clip/1",
    )


# ---------------------------------------------------------------------------
# Config helpers for CLI tests (write minimal site_config + city config)
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, city: City, state_dir: Path) -> None:
    """Write a minimal site_config.yml and feed YAML for CLI tests.

    CLI is invoked with:
        --site-config  tmp_path/site_config.yml
        --config-dir   tmp_path/config
        --output-dir   tmp_path/docs
    Records live under ``state_dir`` (passed explicitly so tests control the path).
    """
    import yaml

    config_dir = tmp_path / "config"
    (config_dir / "feeds").mkdir(parents=True, exist_ok=True)
    (config_dir / "cities").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)

    site_cfg = {
        "base_url": "https://example.com",
        "defaults": {"audio_storage_backend": "local"},
        "state_dir": str(state_dir),
    }
    (tmp_path / "site_config.yml").write_text(yaml.dump(site_cfg))

    # Feed configs live in config_dir/feeds/, not cities/
    feed_cfg = {
        "slug": city.slug,
        "provider": city.provider,
        "source": city.source,
        "podcast_title": city.podcast_title,
        "podcast_author": city.podcast_author,
        "podcast_email": city.podcast_email,
        "podcast_description": city.podcast_description,
    }
    (config_dir / "feeds" / f"{city.slug}.yml").write_text(yaml.dump(feed_cfg))


def _cli(tmp_path, *extra):
    return [
        "rebuild-audio",
        "--site-config",
        str(tmp_path / "site_config.yml"),
        "--config-dir",
        str(tmp_path / "config"),
        "--output-dir",
        str(tmp_path / "docs"),
        *extra,
    ]


# ---------------------------------------------------------------------------
# Identity hash: must be byte-identical to v1 for un-manipulated episodes
# ---------------------------------------------------------------------------


class TestAudioSpecHashV1Compat:
    """The v1 hash format must survive the schema upgrade for identity episodes."""

    def _v1_hash(self, ep, max_kbps=96):
        """Reproduce the exact v1 formula to assert against."""
        import hashlib

        spec = {
            "v": "1",
            "source": ep.video_url,
            "max_kbps": max_kbps,
            "chapters": ep.chapters,
        }
        blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    def test_plain_episode_matches_v1(self):
        ep = _ep()
        assert audio_spec_hash(ep, max_kbps=96) == self._v1_hash(ep)

    def test_episode_with_one_source_still_matches_v1(self):
        ep = _ep()
        ep.sources = [_src()]
        assert audio_spec_hash(ep, max_kbps=96) == self._v1_hash(ep)

    def test_identity_timeline_still_matches_v1(self):
        ep = _ep()
        ep.sources = [_src()]
        ep.timeline = identity_timeline(_src(), 3600.0)
        # identity_timeline digest == "" → still uses v1 format
        assert audio_spec_hash(ep, max_kbps=96) == self._v1_hash(ep)

    def test_summary_change_still_matches_v1(self):
        ep = _ep()
        ep.summary = "A new summary"
        assert audio_spec_hash(ep, max_kbps=96) == self._v1_hash(ep)

    def test_nonce_changes_hash(self):
        ep = _ep()
        base = audio_spec_hash(ep, max_kbps=96)
        ep.audio_rebuild = "fix-pr-1234"
        stamped = audio_spec_hash(ep, max_kbps=96)
        assert stamped != base
        assert stamped != self._v1_hash(ep)

    def test_multi_source_uses_v2_format(self):
        ep = _ep()
        ep.sources = [_src("s0"), _src("s1")]
        assert audio_spec_hash(ep, max_kbps=96) != self._v1_hash(ep)

    def test_non_identity_timeline_uses_v2_format(self):
        ep = _ep()
        tl = Timeline(
            version="silence-v1",
            segments=(
                Segment(
                    served_start=0,
                    served_end=300,
                    kind="source",
                    source_id="s0",
                    source_start=0,
                    source_end=300,
                ),
                Segment(
                    served_start=300,
                    served_end=3300,
                    kind="source",
                    source_id="s0",
                    source_start=600,
                    source_end=3600,
                ),
            ),
        )
        ep.timeline = tl
        ep.sources = [_src()]
        assert audio_spec_hash(ep, max_kbps=96) != self._v1_hash(ep)

    def test_only_stamped_episodes_get_new_key(self):
        from citypods.records import audio_object_key

        city = _city()
        ep_clean = _ep("g1")
        ep_stamped = _ep("g2")
        ep_stamped.audio_rebuild = "fix-pr-9"

        spec_clean = audio_spec_hash(ep_clean, max_kbps=96)
        spec_stamped = audio_spec_hash(ep_stamped, max_kbps=96)
        assert spec_clean != spec_stamped
        assert audio_object_key(city, ep_clean, spec_clean) != audio_object_key(
            city, ep_stamped, spec_stamped
        )


# ---------------------------------------------------------------------------
# Round-trip: episode → record → episode
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_basic_fields_survive_round_trip(self):
        ep = _ep()
        ep2 = record_to_episode(episode_to_record(ep))
        assert ep2.guid == ep.guid
        assert ep2.title == ep.title
        assert ep2.chapters == ep.chapters
        assert ep2.duration == ep.duration

    def test_sources_survive_round_trip(self):
        ep = _ep()
        ep.sources = [_src("s0")]
        ep2 = record_to_episode(episode_to_record(ep))
        assert len(ep2.sources) == 1
        assert ep2.sources[0].id == "s0"
        assert ep2.sources[0].backup_key is None

    def test_timeline_survives_round_trip(self):
        ep = _ep()
        ep.sources = [_src()]
        ep.timeline = Timeline(
            version="silence-v1",
            segments=(
                Segment(
                    served_start=0,
                    served_end=300,
                    kind="source",
                    source_id="s0",
                    source_start=0,
                    source_end=300,
                ),
                Segment(
                    served_start=300,
                    served_end=3300,
                    kind="source",
                    source_id="s0",
                    source_start=600,
                    source_end=3600,
                ),
            ),
        )
        ep2 = record_to_episode(episode_to_record(ep))
        assert ep2.timeline is not None
        assert ep2.timeline.version == "silence-v1"
        assert len(ep2.timeline.segments) == 2
        assert ep2.timeline.segments[1].source_start == 600

    def test_identity_timeline_none_round_trips_as_none(self):
        ep = _ep()
        ep.timeline = None
        rec = episode_to_record(ep)
        assert rec["timeline"] is None
        assert record_to_episode(rec).timeline is None

    def test_audio_rebuild_nonce_round_trips(self):
        ep = _ep()
        ep.audio_rebuild = "fix-pr-42"
        rec = episode_to_record(ep)
        assert rec["audio"]["rebuild"] == "fix-pr-42"
        assert record_to_episode(rec).audio_rebuild == "fix-pr-42"

    def test_empty_nonce_omitted_from_record(self):
        ep = _ep()
        ep.audio_rebuild = ""
        assert episode_to_record(ep)["audio"].get("rebuild") is None

    def test_chapters_basis_round_trips(self):
        ep = _ep()
        ep.chapters_basis = "served"
        assert record_to_episode(episode_to_record(ep)).chapters_basis == "served"

    def test_encode_time_round_trips(self):
        ep = _ep()
        ep.audio_encode_time = "2026-06-03T12:00:00+00:00"
        assert record_to_episode(episode_to_record(ep)).audio_encode_time == ep.audio_encode_time

    def test_duration_served_round_trips(self):
        ep = _ep()
        ep.audio_duration_served = 3150.0
        assert record_to_episode(episode_to_record(ep)).audio_duration_served == 3150.0


# ---------------------------------------------------------------------------
# Lazy v1→v2 upgrade
# ---------------------------------------------------------------------------


class TestLazyV1Upgrade:
    def _v1_rec(self):
        return {
            "uid": "abc",
            "provider_guid": "g1",
            "title": "Meeting",
            "published": "2026-05-19T16:00:00+00:00",
            "video_url": "https://x/g1.mp4",
            "audio": {},
        }

    def test_v1_sources_defaults_to_empty(self):
        assert record_to_episode(self._v1_rec()).sources == []

    def test_v1_timeline_defaults_to_none(self):
        assert record_to_episode(self._v1_rec()).timeline is None

    def test_v1_chapters_basis_defaults_to_source_s0(self):
        assert record_to_episode(self._v1_rec()).chapters_basis == "source:s0"

    def test_v1_spec_hash_stays_valid_after_upgrade(self):
        ep_orig = _ep()
        old_hash = audio_spec_hash(ep_orig, max_kbps=96)

        v1_rec = {
            "uid": "uid-g1",
            "provider_guid": "g1",
            "title": "Council Meeting",
            "published": "2026-05-19T16:00:00+00:00",
            "video_url": "https://example.granicus.com/clip/1.mp4",
            "duration": 3600,
            "audio": {"spec_hash": old_hash},
            "chapters": [{"start": 0, "title": "Call to order"}],
        }
        ep_loaded = record_to_episode(v1_rec)
        assert audio_spec_hash(ep_loaded, max_kbps=96) == old_hash


# ---------------------------------------------------------------------------
# Duration semantics
# ---------------------------------------------------------------------------


class TestDurationSemantics:
    def _fake_ffmpeg(self):
        class FF:
            def extract_audio(self, url, dest, chapters=None):
                dest.write_bytes(b"fake-m4a")

        return FF()

    def test_audio_duration_served_set_on_encode(self, tmp_path):
        from citypods.media import materialize_audio
        from citypods.storage.local import LocalStorage

        city = _city()
        ep = _ep()
        ep.uid = "uid-dur"
        ep.duration = 3600
        ep.media_kind = "hls"

        materialize_audio(
            city,
            [ep],
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=self._fake_ffmpeg(),
            max_kbps=96,
            resolve_media_url=lambda e: e.video_url,
        )
        assert ep.audio_duration_served == 3600.0

    def test_encode_time_set_on_encode(self, tmp_path):
        from citypods.media import materialize_audio
        from citypods.storage.local import LocalStorage

        city = _city()
        ep = _ep()
        ep.uid = "uid-time"
        ep.media_kind = "hls"

        materialize_audio(
            city,
            [ep],
            storage=LocalStorage(root=tmp_path / "a", url_prefix="https://cdn/"),
            ffmpeg=self._fake_ffmpeg(),
            max_kbps=96,
            resolve_media_url=lambda e: e.video_url,
        )
        assert ep.audio_encode_time is not None
        dt = datetime.fromisoformat(ep.audio_encode_time)
        assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# rebuild-audio CLI — nonce stamping
# ---------------------------------------------------------------------------


def _make_rec(uid, encode_time=None, body="City Council"):
    return {
        "uid": uid,
        "provider_guid": f"pg-{uid}",
        "title": "Meeting",
        "published": "2026-05-19T16:00:00+00:00",
        "video_url": f"https://x/{uid}.mp4",
        "body": body,
        "audio": {
            "key": f"granicus/src/{uid}-abc.m4a",
            "url": f"https://cdn/{uid}-abc.m4a",
            "spec_hash": "abc123",
            "encode_time": encode_time,
        },
    }


class TestRebuildAudioNonce:
    def _setup(self, tmp_path, recs):
        city = _city("demo-tx")
        key = source_key(city)
        state_dir = tmp_path / "state"
        save_records(state_dir, key, recs)
        _write_config(tmp_path, city, state_dir)
        return key, state_dir

    def test_stamps_nonce_on_matching_uid(self, tmp_path):
        key, state_dir = self._setup(
            tmp_path,
            {
                "uid-a": _make_rec("uid-a"),
                "uid-b": _make_rec("uid-b"),
            },
        )
        assert cli_main(_cli(tmp_path, "--uid", "uid-a", "--reason", "fix-pr-999")) == 0

        updated = load_records(state_dir, key)
        assert updated["uid-a"]["audio"]["rebuild"] == "fix-pr-999"
        assert updated["uid-b"]["audio"].get("rebuild") is None

    def test_date_range_stamps_only_within_window(self, tmp_path):
        key, state_dir = self._setup(
            tmp_path,
            {
                "uid-early": _make_rec("uid-early", encode_time="2026-06-01T00:00:00+00:00"),
                "uid-in": _make_rec("uid-in", encode_time="2026-06-05T00:00:00+00:00"),
                "uid-late": _make_rec("uid-late", encode_time="2026-06-10T00:00:00+00:00"),
            },
        )
        assert (
            cli_main(
                _cli(
                    tmp_path,
                    "--encoded-after",
                    "2026-06-03",
                    "--encoded-before",
                    "2026-06-07",
                    "--reason",
                    "fix-pr-42",
                )
            )
            == 0
        )

        updated = load_records(state_dir, key)
        assert updated["uid-early"]["audio"].get("rebuild") is None
        assert updated["uid-in"]["audio"]["rebuild"] == "fix-pr-42"
        assert updated["uid-late"]["audio"].get("rebuild") is None

    def test_open_ended_after_bound(self, tmp_path):
        key, state_dir = self._setup(
            tmp_path,
            {
                "uid-old": _make_rec("uid-old", encode_time="2026-05-01T00:00:00+00:00"),
                "uid-new": _make_rec("uid-new", encode_time="2026-06-05T00:00:00+00:00"),
            },
        )
        cli_main(_cli(tmp_path, "--encoded-after", "2026-06-01", "--reason", "fix-55"))

        updated = load_records(state_dir, key)
        assert updated["uid-old"]["audio"].get("rebuild") is None
        assert updated["uid-new"]["audio"]["rebuild"] == "fix-55"

    def test_missing_encode_time_excluded_from_date_range(self, tmp_path):
        key, state_dir = self._setup(
            tmp_path,
            {
                "uid-none": _make_rec("uid-none", encode_time=None),
            },
        )
        cli_main(_cli(tmp_path, "--encoded-after", "2026-01-01", "--reason", "fix"))

        assert load_records(state_dir, key)["uid-none"]["audio"].get("rebuild") is None

    def test_dry_run_does_not_write(self, tmp_path):
        key, state_dir = self._setup(
            tmp_path,
            {
                "uid-a": _make_rec("uid-a", encode_time="2026-06-05T00:00:00+00:00"),
            },
        )
        cli_main(_cli(tmp_path, "--uid", "uid-a", "--reason", "fix", "--dry-run"))

        assert load_records(state_dir, key)["uid-a"]["audio"].get("rebuild") is None

    def test_error_when_neither_reason_nor_drop_object(self, tmp_path, capsys):
        city = _city("demo-tx")
        _write_config(tmp_path, city, tmp_path / "state")
        rc = cli_main(_cli(tmp_path, "--uid", "uid-a"))
        assert rc == 1
        assert "required" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# rebuild-audio CLI — drop-object
# ---------------------------------------------------------------------------


class TestRebuildAudioDropObject:
    def test_drop_object_clears_audio_pointer(self, tmp_path):
        city = _city("demo-tx")
        key = source_key(city)
        state_dir = tmp_path / "state"
        obj_key = "granicus/src/abc-123.m4a"

        save_records(
            state_dir,
            key,
            {
                "uid-bad": {
                    "uid": "uid-bad",
                    "provider_guid": "pg-bad",
                    "title": "Meeting",
                    "published": "2026-05-19T16:00:00+00:00",
                    "video_url": "https://x/bad.mp4",
                    "audio": {
                        "key": obj_key,
                        "url": "https://cdn/bad.m4a",
                        "spec_hash": "abc123def456",
                        "encode_time": "2026-06-01T00:00:00+00:00",
                    },
                }
            },
        )
        _write_config(tmp_path, city, state_dir)

        audio_dir = tmp_path / "docs" / "audio"
        (audio_dir / Path(obj_key).parent).mkdir(parents=True, exist_ok=True)
        (audio_dir / obj_key).write_bytes(b"corrupt-audio")

        assert cli_main(_cli(tmp_path, "--uid", "uid-bad", "--drop-object")) == 0

        rec_audio = load_records(state_dir, key)["uid-bad"]["audio"]
        assert rec_audio.get("key") is None
        assert rec_audio.get("url") is None
        assert rec_audio.get("spec_hash") is None
        assert not (audio_dir / obj_key).exists()
