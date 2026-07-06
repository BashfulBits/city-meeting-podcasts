"""Tests for `citypods compute reclaim-transcript` (GH#833 recovery tool).

Re-adopts an ASR artifact already uploaded to storage whose record `transcript` block was lost
(e.g. the owned-block-merge bug fixed in #833) -- never re-transcribes, only re-attaches existing
keys when they're actually present at the recomputed recipe hash.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from citypods import cli
from citypods.models import City, Episode
from citypods.records import episode_to_record, record_to_episode, save_records, source_key
from citypods.stages import _asr_object_key, _asr_recipe_hash, _asr_words_object_key
from citypods.storage.local import LocalStorage


def _city(**overrides) -> City:
    values = {
        "slug": "t-tx",
        "provider": "swagit",
        "source": {"feed_url": "x"},
        "podcast_title": "T",
        "podcast_author": "City of T",
        "podcast_email": "",
        "podcast_description": "d",
    }
    values.update(overrides)
    return City(**values)


def _ep(uid: str = "u1") -> Episode:
    return Episode(
        guid="g1",
        uid=uid,
        title="Meeting",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/vid.mp4",
        media_kind="direct",
        duration=3600,
    )


def _args(tmp_path: Path, *, source_key_: str, episode_uid: str, write: bool) -> argparse.Namespace:
    return argparse.Namespace(
        source_key=source_key_,
        episode_uid=episode_uid,
        write=write,
        site_config="unused",
        config_dir="unused",
        output_dir=str(tmp_path / "docs"),
        base_url="https://cdn.example",
    )


def _seed(tmp_path: Path, city: City, ep: Episode) -> tuple[LocalStorage, str, str]:
    """Seed the ASR artifact in storage + the transcript-less record on 'remote', matching what a
    real run leaves behind after GH#833's bug: artifact uploaded, record never updated."""
    sk = source_key(city)
    recipe = _asr_recipe_hash(city, ep, None)
    asr_key = _asr_object_key(sk, ep.uid, recipe)
    words_key = _asr_words_object_key(sk, ep.uid, recipe)

    storage = LocalStorage(root=tmp_path / "docs" / "audio", url_prefix="https://cdn/audio")
    vtt = tmp_path / "seed.vtt"
    vtt.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n")
    words = tmp_path / "seed.words.json"
    words.write_text('{"schema":"1","basis":"served","segments":[]}')
    storage.put_file(asr_key, vtt, "text/vtt")
    storage.put_file(words_key, words, "application/json")

    state_dir = tmp_path / "state"
    save_records(state_dir, sk, {ep.uid: episode_to_record(ep)})
    from citypods.statesync import push_state

    push_state(storage, state_dir)
    return storage, sk, recipe


def _patch_config(monkeypatch, city: City) -> None:
    monkeypatch.setattr(cli, "load_site_config", lambda path: {})
    monkeypatch.setattr(cli, "load_city_configs", lambda config_dir, defaults: [city])


def test_reclaim_transcript_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    city = _city()
    ep = _ep("u1")
    _patch_config(monkeypatch, city)
    storage, sk, recipe = _seed(tmp_path, city, ep)

    rc = cli._compute_reclaim_transcript(
        _args(tmp_path, source_key_=sk, episode_uid="u1", write=False)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "found existing artifact" in out
    assert recipe in out
    assert "dry run" in out
    # the record on "remote" is unchanged
    from citypods.statesync import fetch_remote_records

    remote = fetch_remote_records(storage, sk)
    assert remote["u1"].get("transcript") is None


def test_reclaim_transcript_write_adopts_and_pushes(tmp_path, monkeypatch, capsys):
    city = _city()
    ep = _ep("u1")
    _patch_config(monkeypatch, city)
    storage, sk, recipe = _seed(tmp_path, city, ep)

    rc = cli._compute_reclaim_transcript(
        _args(tmp_path, source_key_=sk, episode_uid="u1", write=True)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "reclaimed transcript" in out

    from citypods.statesync import fetch_remote_records

    remote = fetch_remote_records(storage, sk)
    transcript = remote["u1"]["transcript"]
    assert transcript is not None
    assert transcript["spec_hash"] == recipe
    assert transcript["synced"] is True
    reclaimed_ep = record_to_episode(remote["u1"])
    assert reclaimed_ep.transcript_key
    assert reclaimed_ep.transcript_words_key


def test_reclaim_transcript_errors_when_no_artifact_exists(tmp_path, monkeypatch, capsys):
    city = _city()
    ep = _ep("u1")
    _patch_config(monkeypatch, city)
    sk = source_key(city)
    state_dir = tmp_path / "state"
    save_records(state_dir, sk, {"u1": episode_to_record(ep)})
    storage = LocalStorage(root=tmp_path / "docs" / "audio", url_prefix="https://cdn/audio")
    from citypods.statesync import push_state

    push_state(storage, state_dir)
    # no artifact ever seeded at the recipe key

    rc = cli._compute_reclaim_transcript(
        _args(tmp_path, source_key_=sk, episode_uid="u1", write=True)
    )

    assert rc == 1
    assert "nothing to reclaim" in capsys.readouterr().out


def test_reclaim_transcript_noop_when_record_already_has_transcript(tmp_path, monkeypatch, capsys):
    city = _city()
    ep = _ep("u1")
    ep.transcript_key = "already/there.vtt"
    ep.transcript_words_key = "already/there.words.json"
    _patch_config(monkeypatch, city)
    sk = source_key(city)
    state_dir = tmp_path / "state"
    save_records(state_dir, sk, {"u1": episode_to_record(ep)})
    storage = LocalStorage(root=tmp_path / "docs" / "audio", url_prefix="https://cdn/audio")
    from citypods.statesync import push_state

    push_state(storage, state_dir)

    rc = cli._compute_reclaim_transcript(
        _args(tmp_path, source_key_=sk, episode_uid="u1", write=True)
    )

    assert rc == 0
    assert "nothing to do" in capsys.readouterr().out


def test_reclaim_transcript_errors_on_unknown_source(tmp_path, monkeypatch):
    _patch_config(monkeypatch, _city())
    rc = cli._compute_reclaim_transcript(
        _args(tmp_path, source_key_="does-not-exist", episode_uid="u1", write=False)
    )
    assert rc == 1


def test_reclaim_transcript_errors_on_missing_record(tmp_path, monkeypatch):
    city = _city()
    _patch_config(monkeypatch, city)
    sk = source_key(city)
    state_dir = tmp_path / "state"
    save_records(state_dir, sk, {})
    storage = LocalStorage(root=tmp_path / "docs" / "audio", url_prefix="https://cdn/audio")
    from citypods.statesync import push_state

    push_state(storage, state_dir)

    rc = cli._compute_reclaim_transcript(
        _args(tmp_path, source_key_=sk, episode_uid="missing-uid", write=False)
    )
    assert rc == 1
