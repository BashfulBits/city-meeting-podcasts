"""review/61 (GH#2707): video slide chapter images."""

from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from citypods import slides as sl
from citypods.feeds import chapters_json, chapters_url
from citypods.models import Episode
from citypods.records import (
    episode_to_record,
    feed_content_hash,
    record_to_episode,
    referenced_audio_keys,
)
from citypods.stages import SlidesStage, StageContext, slides_eligible
from citypods.timeline import Segment, SourceMedia, Timeline

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _slide_image(seed: int = 0, size=(640, 360)) -> Image.Image:
    """A synthetic slide: white page, a title bar and text-like rows that differ per seed."""
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, size[0], 50), fill=(20, 60, 140))
    for row in range(8):
        width = 120 + ((seed * 37 + row * 53) % 380)
        top = 80 + row * 32
        draw.rectangle((40, top, 40 + width, top + 12), fill=(30, 30, 30))
    draw.ellipse((size[0] - 160 - seed * 7 % 60, 120, size[0] - 40, 240), fill=(200, 40, 40))
    return image


def _flat_image(color=(0, 0, 0)) -> Image.Image:
    return Image.new("RGB", (640, 360), color)


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def _episode(**overrides) -> Episode:
    values = {
        "guid": "g1",
        "uid": "uid-1",
        "title": "Council",
        "published": datetime(2026, 11, 1, tzinfo=UTC),
        "video_url": "https://video.example.com/meeting.mp4",
    }
    values.update(overrides)
    return Episode(**values)


def _ready_block(*items) -> dict:
    return {"status": "ready", "recipe": sl.SLIDES_RECIPE, "items": list(items)}


def _item(start, end, url="https://cdn.example.com/slides/uid-1/a.jpg", source_id="s0"):
    return {
        "source_id": source_id,
        "source_start": start,
        "source_end": end,
        "key": "slides/uid-1/a.jpg",
        "url": url,
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_on_by_default_and_both_switches_turn_it_off():
    assert sl.effective_config(None, None)["enabled"] is True
    assert sl.effective_config({"enabled": False}, None)["enabled"] is False
    assert sl.effective_config(None, {"enabled": False})["enabled"] is False
    # A city cannot re-enable what the site kill switch turned off.
    assert sl.effective_config({"enabled": False}, {"enabled": True})["enabled"] is False


def test_city_overrides_site_and_types_are_normalized():
    config = sl.effective_config(
        {"min_duration_seconds": 20, "max_episodes_per_run": 5},
        {"min_duration_seconds": 30, "exclude_dhashes": ["00ff00ff00ff00ff"]},
    )
    assert config["min_duration_seconds"] == 30.0
    assert config["max_episodes_per_run"] == 5
    assert config["exclude_dhashes"] == ["00ff00ff00ff00ff"]
    assert sl.effective_config(None, {"min_edge_density": None})["min_edge_density"] == 0.0


@pytest.mark.parametrize(
    "site,city",
    [
        ({"bogus": 1}, None),
        ({"enabled": "yes"}, None),
        ({"decode": "all"}, None),
        ({"max_slides_per_episode": 0}, None),
        ({"max_slides_per_episode": 2.5}, None),
        ({"edge_trim_seconds": -1}, None),
        ({"min_duration_seconds": True}, None),
        ({"exclude_dhashes": ["00ff00ff00ff00ff"]}, None),  # per-city only
        (None, {"exclude_dhashes": ["xyz"]}),
        ("not-a-mapping", None),
    ],
)
def test_malformed_config_is_rejected(site, city):
    with pytest.raises(sl.SlidesConfigError):
        sl.effective_config(site, city)


def test_committed_site_config_block_is_valid_and_enabled():
    import yaml

    site = yaml.safe_load(Path("config/site_config.yml").read_text())
    assert sl.effective_config(site["slides"], None)["enabled"] is True


def test_detection_fingerprint_ignores_run_caps():
    base = sl.effective_config(None, None)
    capped = sl.effective_config({"max_episodes_per_run": 1, "max_video_gb_per_run": 1}, None)
    assert sl.detection_fingerprint(base) == sl.detection_fingerprint(capped)
    tuned = sl.effective_config({"noise_db": -40}, None)
    assert sl.detection_fingerprint(base) != sl.detection_fingerprint(tuned)


# ---------------------------------------------------------------------------
# ffmpeg commands and parsing
# ---------------------------------------------------------------------------


def test_detect_command_streams_keyframes_with_whitelist():
    command = sl.detect_command("ffmpeg", "https://v.example.com/a.m3u8", sl.DEFAULTS)
    assert command[command.index("-protocol_whitelist") + 1] == "crypto,data,http,https,tcp,tls"
    assert "file" not in command[command.index("-protocol_whitelist") + 1].split(",")
    assert command[command.index("-skip_frame") + 1] == "nokey"
    assert command.index("-skip_frame") < command.index("-i")
    vf = command[command.index("-vf") + 1]
    assert vf == "scale=320:-2,format=gray,freezedetect=n=-50dB:d=15,metadata=print:file=-"
    assert command[-3:] == ["-f", "null", "-"]


def test_detect_command_fps1_mode_decodes_everything():
    command = sl.detect_command("ffmpeg", "/tmp/a.mp4", {**sl.DEFAULTS, "decode": "fps1"})
    assert "-skip_frame" not in command
    assert "-protocol_whitelist" not in command  # local paths need no remote args
    assert command[command.index("-vf") + 1].startswith("fps=1,scale=320")


def test_capture_command_seeks_before_input_and_emits_one_png():
    command = sl.capture_command("ffmpeg", "https://v.example.com/a.mp4", 12.3456)
    assert command[command.index("-ss") + 1] == "12.346"
    assert command.index("-ss") < command.index("-i")
    assert command[command.index("-frames:v") + 1] == "1"
    assert command[-3:] == ["-vcodec", "png", "-"]


def test_parse_freeze_intervals():
    text = "\n".join(
        [
            "frame:0 pts:0 pts_time:0",
            "lavfi.freezedetect.freeze_start=10.5",
            "lavfi.freezedetect.freeze_duration=19.5",
            "lavfi.freezedetect.freeze_end=30",
            "lavfi.freezedetect.freeze_end=40",  # end with no open start: ignored
            "lavfi.freezedetect.freeze_start=50",
            "lavfi.freezedetect.freeze_end=45",  # reversed: skipped
            "lavfi.freezedetect.freeze_start=100",
        ]
    )
    assert sl.parse_freeze_intervals(text, 160.0) == [
        sl.Interval(10.5, 30.0),
        sl.Interval(100.0, 160.0),
    ]
    # Without a duration a trailing open start is dropped.
    assert sl.parse_freeze_intervals(text) == [sl.Interval(10.5, 30.0)]
    assert sl.parse_freeze_intervals("") == []


def test_settle_time_is_a_few_seconds_in():
    assert sl.settle_time(sl.Interval(100, 200)) == 103
    assert sl.settle_time(sl.Interval(100, 106)) == 102


def test_edge_trim_drops_holding_and_end_slates():
    intervals = [
        sl.Interval(0, 60),  # holding slate before the first chapter
        sl.Interval(90, 140),  # inside the leading edge but after the first chapter: kept
        sl.Interval(1000, 1100),
        sl.Interval(3500, 3600),  # end slate
    ]
    kept, rejected = sl.edge_trim(
        intervals, duration=3600, edge_seconds=120, first_chapter_start=80
    )
    assert kept == [sl.Interval(90, 140), sl.Interval(1000, 1100)]
    assert rejected == 2
    kept, _ = sl.edge_trim(intervals, duration=None, edge_seconds=120, first_chapter_start=None)
    assert kept == [sl.Interval(1000, 1100), sl.Interval(3500, 3600)]


def test_parse_probe():
    probe = sl.parse_probe(
        json.dumps(
            {
                "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
                "format": {"duration": "3600.0", "bit_rate": "2000000"},
            }
        )
    )
    assert probe == sl.ProbeResult(3600.0, 2_000_000, True)
    assert probe.estimated_bytes == 900_000_000
    assert sl.parse_probe('{"streams": [], "format": {}}').has_video is False
    assert sl.parse_probe("not json") == sl.ProbeResult(None, None, False)
    # Unknown duration / bit rate: a conservative fallback, never a zero-byte reservation.
    assert sl.parse_probe('{"format": {"duration": "x"}}').estimated_bytes == 4 * 3600 * 1_000_000
    assert sl.ProbeResult(100.0, None, True).estimated_bytes == 100_000_000


# ---------------------------------------------------------------------------
# Pixel checks, hashing, encoding
# ---------------------------------------------------------------------------


def test_flat_frames_are_rejected_and_slides_kept():
    config = sl.effective_config(None, None)
    assert sl.pixel_rejection(sl.frame_metrics(_flat_image()), config) == "flat"
    assert sl.pixel_rejection(sl.frame_metrics(_slide_image()), config) is None


def test_low_detail_frames_are_rejected():
    # A smooth gradient: high luma spread but no sharp edges (a soft, out-of-focus room shot).
    image = Image.linear_gradient("L").resize((640, 360))
    metrics = sl.frame_metrics(image)
    config = sl.effective_config(None, None)
    assert metrics.luma_stddev > config["min_luma_stddev"]
    assert sl.pixel_rejection(metrics, config) == "low_detail"
    assert sl.pixel_rejection(metrics, {**config, "min_edge_density": 0.0}) is None


def test_excluded_dhash_rejects_a_known_slate():
    metrics = sl.frame_metrics(_slide_image(1))
    config = sl.effective_config(None, {"exclude_dhashes": [sl.dhash_hex(metrics.dhash)]})
    assert sl.pixel_rejection(metrics, config) == "excluded"


def test_dhash_is_stable_under_rescale_and_distinct_across_slides():
    a = sl.dhash(_slide_image(1))
    assert sl.hamming(a, sl.dhash(_slide_image(1).resize((1280, 720)))) <= 6
    assert sl.hamming(a, sl.dhash(_slide_image(5))) > 6
    assert len(sl.dhash_hex(a)) == 16


def test_encode_slide_pads_to_720p_without_crop_and_is_bounded():
    data = sl.encode_slide(_slide_image(size=(800, 600)), max_kb=150)
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG"
    assert image.size == sl.IMAGE_SIZE
    assert len(data) <= 150 * 1024
    # 4:3 source padded with black side bars, not cropped.
    assert image.convert("RGB").getpixel((5, 360)) == pytest.approx((0, 0, 0), abs=12)


def test_encode_slide_steps_quality_down_to_the_floor():
    noisy = Image.effect_noise((1280, 720), 120).convert("RGB")
    tiny = sl.encode_slide(noisy, max_kb=1)
    assert len(tiny) > 1024  # cannot reach 1 KB; stops at the quality floor instead of looping
    assert len(tiny) < len(sl.encode_slide(noisy, max_kb=10_000))


def test_slide_key_is_content_addressed():
    assert sl.slide_key("u", b"abc") == sl.slide_key("u", b"abc")
    assert sl.slide_key("u", b"abc") != sl.slide_key("u", b"abd")
    assert sl.slide_key("u", b"abc").startswith("slides/u/")
    assert sl.slide_key("u", b"abc").endswith(".jpg")


def test_decode_png_tolerates_garbage():
    assert sl.decode_png(b"") is None
    assert sl.decode_png(b"not a png") is None
    assert sl.decode_png(_png(_slide_image())).size == (640, 360)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _candidate(seed, start, end, source_id="s0"):
    return sl.Candidate(
        source_id, sl.Interval(start, end), sl.frame_metrics(_slide_image(seed)), None
    )


def test_select_slides_merges_consecutive_duplicates():
    kept, rejected, capped = sl.select_slides(
        [_candidate(1, 100, 130), _candidate(1, 131, 160), _candidate(2, 200, 230)],
        max_recurrence=3,
        max_slides=50,
    )
    assert [(c.interval.start, c.interval.end) for c in kept] == [(100, 160), (200, 230)]
    assert rejected == {"duplicate": 1}
    assert capped is False


def test_select_slides_keeps_a_slide_shown_again_much_later_separate():
    kept, rejected, _ = sl.select_slides(
        [_candidate(1, 300, 330), _candidate(1, 900, 930)],
        max_recurrence=3,
        max_slides=50,
    )
    assert [(c.interval.start, c.interval.end) for c in kept] == [(300, 330), (900, 930)]
    assert rejected == {}


def test_select_slides_drops_recurring_frames():
    kept, rejected, _ = sl.select_slides(
        [
            _candidate(9, 100, 130),
            _candidate(1, 200, 230),
            _candidate(9, 300, 330),
            _candidate(2, 400, 430),
            _candidate(9, 500, 530),
        ],
        max_recurrence=3,
        max_slides=50,
    )
    assert [c.interval.start for c in kept] == [200, 400]
    assert rejected == {"recurrence": 3}


def test_select_slides_caps_and_does_not_merge_across_sources():
    kept, _, capped = sl.select_slides(
        [_candidate(1, 100, 130, "s0"), _candidate(1, 10, 40, "s1"), _candidate(2, 200, 230)],
        max_recurrence=3,
        max_slides=2,
    )
    assert [(c.source_id, c.interval.start) for c in kept] == [("s0", 100), ("s0", 200)]
    assert capped is True


def test_run_budget_caps_episodes_and_bytes():
    budget = sl.SlidesRunBudget(max_episodes=1, max_bytes=100)
    assert budget.admit_episode() is True
    assert budget.admit_episode() is False
    assert budget.admit_bytes(500) is True  # the first source always fits
    assert budget.admit_bytes(1) is False
    from_config = sl.SlidesRunBudget.from_config(sl.effective_config(None, None))
    assert from_config.max_episodes == 20
    assert from_config.max_bytes == 40 * 1024**3


# ---------------------------------------------------------------------------
# Served clock and chapters JSON
# ---------------------------------------------------------------------------


def test_served_slides_use_source_time_on_identity_timeline():
    ep = _episode(slides=_ready_block(_item(100, 160), {"url": 5}, _item(10, 5)))
    assert sl.episode_served_slides(ep) == [
        {"start": 100.0, "end": 160.0, "url": "https://cdn.example.com/slides/uid-1/a.jpg"}
    ]


def test_only_ready_blocks_serve_slides():
    for status in ("failed", "none-found", "no-video"):
        assert sl.episode_served_slides(_episode(slides={"status": status, "items": []})) == []
    assert sl.episode_served_slides(_episode(slides={})) == []


def test_served_slides_remap_through_a_trimmed_timeline():
    source = SourceMedia("s0", "swagit", "ref", "direct", 3600.0, None)
    timeline = Timeline(
        version="1",
        segments=(
            Segment(0, 10, "insert", insert="intro", asset_id="intro", asset_version="1"),
            # The first 600 s of source were trimmed (pre-meeting dead air).
            Segment(10, 3010, "source", source_id="s0", source_start=600, source_end=3600),
        ),
    )
    ep = _episode(
        sources=[source],
        timeline=timeline,
        slides=_ready_block(
            _item(100, 200, url="https://cdn.example.com/cut.jpg"),  # inside the cut span
            _item(1000, 1060, url="https://cdn.example.com/kept.jpg"),
        ),
    )
    served = sl.episode_served_slides(ep)
    assert [row["url"] for row in served] == ["https://cdn.example.com/kept.jpg"]
    assert served[0]["start"] == pytest.approx(410)
    assert served[0]["end"] == pytest.approx(470)


def test_chapters_with_slides_matches_agenda_and_adds_sub_chapters():
    chapters = [
        {"start": 0, "title": "Call to order"},
        {"start": 300, "title": "Budget presentation"},
        {"start": 900, "title": "Public comment"},
    ]
    slides = [
        {"start": 301.4, "end": 360, "url": "https://c/1.jpg"},  # matches the agenda chapter
        {"start": 360, "end": 420, "url": "https://c/2.jpg"},  # back to back: no reset between
        {"start": 500, "end": 560, "url": "https://c/3.jpg"},  # reset after (gap to 900)
    ]
    assert sl.chapters_with_slides(chapters, slides) == [
        {"startTime": 0, "title": "Call to order"},
        {"startTime": 300, "title": "Budget presentation", "img": "https://c/1.jpg"},
        {
            "startTime": 360,
            "endTime": 420,
            "title": "Budget presentation",
            "img": "https://c/2.jpg",
            "toc": False,
        },
        {"startTime": 420, "title": "Budget presentation", "toc": False},
        {
            "startTime": 500,
            "endTime": 560,
            "title": "Budget presentation",
            "img": "https://c/3.jpg",
            "toc": False,
        },
        {"startTime": 560, "title": "Budget presentation", "toc": False},
        {"startTime": 900, "title": "Public comment"},
    ]


def test_chapters_with_slides_without_agenda_uses_fallback_title():
    assert sl.chapters_with_slides([], [{"start": 50, "end": 80, "url": "https://c/1.jpg"}]) == [
        {
            "startTime": 50,
            "endTime": 80,
            "title": "Presentation",
            "img": "https://c/1.jpg",
            "toc": False,
        },
        {"startTime": 80, "title": "Presentation", "toc": False},
    ]


def test_chapters_json_unchanged_without_slides_and_merged_with_them():
    ep = _episode(chapters=[{"start": 0, "title": "Call to order"}])
    plain = json.loads(chapters_json(ep))
    assert plain == {"version": "1.2.0", "chapters": [{"startTime": 0, "title": "Call to order"}]}
    ep.slides = _ready_block(_item(400, 460))
    merged = json.loads(chapters_json(ep))["chapters"]
    assert merged[1]["img"] == "https://cdn.example.com/slides/uid-1/a.jpg"
    assert merged[1]["toc"] is False


def test_chapters_url_exists_for_slides_only_episodes():
    city = SimpleNamespace(slug="x-tx")
    ep = _episode()
    assert chapters_url(city, ep, "https://pods.example.com") is None
    ep.slides = _ready_block(_item(400, 460))
    assert chapters_url(city, ep, "https://pods.example.com/") == (
        "https://pods.example.com/x-tx/chapters/uid-1.json"
    )


# ---------------------------------------------------------------------------
# Records, hashes, GC
# ---------------------------------------------------------------------------


def test_slides_block_round_trips_and_changes_only_the_feed_hash():
    from citypods.records import audio_spec_hash

    ep = _episode(slides=_ready_block(_item(400, 460)))
    record = episode_to_record(ep)
    assert record["slides"]["items"][0]["key"] == "slides/uid-1/a.jpg"
    assert record_to_episode(record).slides == ep.slides
    assert episode_to_record(_episode())["slides"] is None
    assert record_to_episode(episode_to_record(_episode())).slides == {}

    bare = _episode()
    assert feed_content_hash([ep], "fp") != feed_content_hash([bare], "fp")
    assert audio_spec_hash(ep, max_kbps=96) == audio_spec_hash(bare, max_kbps=96)


def test_slide_keys_are_referenced_for_gc(tmp_path):
    ep = _episode(slides=_ready_block(_item(400, 460)))
    path = tmp_path / "sources" / "x-tx" / "episodes.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"episodes": {"uid-1": episode_to_record(ep)}}))
    assert "slides/uid-1/a.jpg" in referenced_audio_keys(tmp_path)


def test_gc_treats_slides_jpgs_as_managed():
    from scripts.gc_audio import is_managed_artifact

    assert is_managed_artifact("slides/uid-1/abc.jpg") is True
    assert is_managed_artifact("slides/uid-1/notes.txt") is False


def test_slides_lane_is_selectable_from_the_cli(monkeypatch):
    from citypods import cli

    captured = {}
    monkeypatch.setattr(cli, "build", lambda **kw: captured.update(kw) or [])
    assert cli.main(["enrich", "--lane", "slides", "--dry-run"]) == 0
    assert captured["lane"] == "slides"


# ---------------------------------------------------------------------------
# SlidesStage
# ---------------------------------------------------------------------------


class _Storage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def exists(self, key):
        return key in self.objects

    def put_file(self, key, local_path, content_type):
        assert content_type == "image/jpeg"
        self.puts += 1
        self.objects[key] = Path(local_path).read_bytes()
        return self.public_url(key)

    def public_url(self, key):
        return f"https://b2.example.com/{key}"


class _Provider:
    def resolve_media_url(self, ep, source):
        return ep.video_url


class _FakeFfmpeg:
    """Answers probe / detect / capture commands from canned data."""

    def __init__(self, *, freeze_text, frames, has_video=True, fail=False):
        self.freeze_text = freeze_text
        self.frames = frames  # settle-time -> PIL image
        self.has_video = has_video
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.fail:
            raise subprocess.CalledProcessError(1, command)
        if command[0].endswith("ffprobe"):
            streams = [{"codec_type": "video"}] if self.has_video else []
            out = json.dumps(
                {"streams": streams, "format": {"duration": "3600", "bit_rate": "1000000"}}
            )
            return SimpleNamespace(stdout=out)
        if "-vf" in command:
            return SimpleNamespace(stdout=self.freeze_text)
        at = float(command[command.index("-ss") + 1])
        return SimpleNamespace(stdout=_png(self.frames.get(at, _flat_image())))


def _ctx(storage, **overrides):
    values = {
        "storage": storage,
        "ffmpeg": SimpleNamespace(binary="ffmpeg"),
        "max_kbps": 96,
        "dry_run": False,
    }
    values.update(overrides)
    return StageContext(**values)


def _city(**extra):
    from tests.test_stages import _city as base_city

    city = base_city()
    city.extra.update(extra)
    return city


_FREEZES = "\n".join(
    [
        "lavfi.freezedetect.freeze_start=10",  # holding slate: edge-trimmed
        "lavfi.freezedetect.freeze_end=100",
        "lavfi.freezedetect.freeze_start=600",
        "lavfi.freezedetect.freeze_end=660",
        "lavfi.freezedetect.freeze_start=661",  # same slide again: merged
        "lavfi.freezedetect.freeze_end=700",
        "lavfi.freezedetect.freeze_start=1200",  # black frame: flat
        "lavfi.freezedetect.freeze_end=1300",
        "lavfi.freezedetect.freeze_start=2000",
        "lavfi.freezedetect.freeze_end=2045",
    ]
)


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    monkeypatch.setattr("citypods.stages.validate_source_url", lambda url, resolve=True: url)


def test_stage_detects_uploads_and_records_source_time_slides():
    storage = _Storage()
    fake = _FakeFfmpeg(
        freeze_text=_FREEZES,
        frames={603.0: _slide_image(1), 664.0: _slide_image(1), 2003.0: _slide_image(2)},
    )
    ep = _episode()
    stats = SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(storage))

    assert stats.errors == []
    assert stats.ran == 1
    assert ep.slides["status"] == "ready"
    assert [(i["source_start"], i["source_end"]) for i in ep.slides["items"]] == [
        (600.0, 700.0),
        (2000.0, 2045.0),
    ]
    assert ep.slides["rejected"] == {"edge_trim": 1, "flat": 1, "duplicate": 1}
    assert ep.slides["recipe"] == sl.SLIDES_RECIPE
    assert ep.slides["streamed_bytes_estimate"] == 450_000_000
    assert storage.puts == 2
    for item in ep.slides["items"]:
        assert item["key"].startswith("slides/uid-1/")
        assert item["url"] == f"https://b2.example.com/{item['key']}"
    assert stats.bytes_written == sum(item["bytes"] for item in ep.slides["items"])

    # Re-running reuses the content-addressed objects.
    SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(storage))
    assert storage.puts == 2


def test_stage_records_no_video_and_none_found():
    storage = _Storage()
    ep = _episode()
    SlidesStage(run=_FakeFfmpeg(freeze_text="", frames={}, has_video=False)).process(
        _Provider(), _city(), [ep], _ctx(storage)
    )
    assert ep.slides["status"] == "no-video"
    ep = _episode()
    SlidesStage(run=_FakeFfmpeg(freeze_text="", frames={})).process(
        _Provider(), _city(), [ep], _ctx(storage)
    )
    assert ep.slides["status"] == "none-found"
    assert ep.slides["items"] == []


def test_stage_failure_retries_then_gives_up():
    fake = _FakeFfmpeg(freeze_text="", frames={}, fail=True)
    ep = _episode()
    for attempt in (1, 2):
        stats = SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(_Storage()))
        assert len(stats.errors) == 1
        assert ep.slides == {
            "status": "failed",
            "recipe": sl.SLIDES_RECIPE,
            "attempts": attempt,
            "reason": "CalledProcessError",
        }
    stats = SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(_Storage()))
    assert stats.errors == []
    assert ep.slides["attempts"] == 3


def test_stage_respects_city_opt_out_eligibility_stop_and_caps():
    fake = _FakeFfmpeg(freeze_text="", frames={})
    storage = _Storage()

    ep = _episode()
    SlidesStage(run=fake).process(
        _Provider(), _city(slides={"enabled": False}), [ep], _ctx(storage)
    )
    assert ep.slides == {} and fake.calls == []

    ep = _episode(video_url=None)
    assert slides_eligible(ep) is False
    SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(storage))
    assert ep.slides == {} and fake.calls == []

    ep = _episode()
    SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(storage, stop=lambda: True))
    assert ep.slides == {} and fake.calls == []

    episodes = [_episode(guid="a", uid="a"), _episode(guid="b", uid="b")]
    ctx = _ctx(storage, slides_config={"max_episodes_per_run": 1})
    SlidesStage(run=fake).process(_Provider(), _city(), episodes, ctx)
    assert episodes[0].slides["status"] == "none-found"
    assert episodes[1].slides == {}

    # The byte budget leaves the second episode untouched for a later run.
    episodes = [_episode(guid="c", uid="c"), _episode(guid="d", uid="d")]
    ctx = _ctx(storage, slides_config={"max_video_gb_per_run": 0.1})
    SlidesStage(run=fake).process(_Provider(), _city(), episodes, ctx)
    assert episodes[0].slides["status"] == "none-found"
    assert episodes[1].slides == {}


def test_stage_reports_malformed_city_config():
    stats = SlidesStage(run=_FakeFfmpeg(freeze_text="", frames={})).process(
        _Provider(), _city(slides={"bogus": 1}), [_episode()], _ctx(_Storage())
    )
    assert stats.errors and "unknown slides key" in stats.errors[0]


def test_stage_does_nothing_on_dry_run_or_without_storage():
    fake = _FakeFfmpeg(freeze_text="", frames={})
    ep = _episode()
    SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(None))
    SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(_Storage(), dry_run=True))
    assert fake.calls == [] and ep.slides == {}


def test_slides_lane_owns_only_its_block():
    from citypods.records import protected_blocks_for_lane

    protected = protected_blocks_for_lane("slides")
    assert "slides" not in protected
    assert {"audio", "transcript", "moments", "judging"} <= protected
    assert "slides" in protected_blocks_for_lane("audio")


def test_stage_rejects_non_https_sources_before_probing(monkeypatch):
    from citypods.security import SecurityError, validate_source_url

    monkeypatch.setattr(
        "citypods.stages.validate_source_url",
        lambda url, resolve=True: validate_source_url(url, resolve=False),
    )
    fake = _FakeFfmpeg(freeze_text="", frames={})
    ep = _episode(video_url="ftp://127.0.0.1/private.mp4")
    stats = SlidesStage(run=fake).process(_Provider(), _city(), [ep], _ctx(_Storage()))
    assert fake.calls == []
    assert ep.slides["status"] == "failed"
    assert ep.slides["reason"] == SecurityError.__name__
    assert stats.errors


def test_stage_reserves_a_multi_source_episode_before_any_detection():
    sources = [
        SourceMedia("s0", "swagit", "https://video.example.com/a.mp4", "direct", 3600.0, None),
        SourceMedia("s1", "swagit", "https://video.example.com/b.mp4", "direct", 3600.0, None),
    ]
    fake = _FakeFfmpeg(freeze_text="", frames={})
    ctx = _ctx(_Storage(), slides_config={"max_video_gb_per_run": 1.0})
    first = _episode(guid="a", uid="a")
    second = _episode(guid="b", uid="b", sources=sources)
    SlidesStage(run=fake).process(_Provider(), _city(), [first, second], ctx)
    assert first.slides["status"] == "none-found"
    assert second.slides == {}
    # Each source alone would fit (0.45 + 0.45 GB) but the episode total does not, so the
    # second episode is probed to size it and never streamed through detection.
    detect_calls = [c for c in fake.calls if "-vf" in c]
    assert len(detect_calls) == 1
