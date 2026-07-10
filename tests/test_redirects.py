"""Tests for stable-URL plumbing: alias redirects + orphan-audio GC."""

from __future__ import annotations

import json

from citypods.models import City
from citypods.records import referenced_audio_keys, save_records
from citypods.run import _write_aliases
from citypods.site import render_redirect_feed, render_redirect_page
from citypods.storage.local import LocalStorage
from tests.conftest import write_local_backend_site_config


def _city(slug="denton-tx-city-council", aliases=None):
    return City(
        slug=slug,
        provider="swagit",
        source={"list_url": "L", "body": "City Council"},
        podcast_title="Denton — City Council",
        podcast_author="City of Denton, TX",
        podcast_email="",
        podcast_description="d",
        aliases=aliases or [],
    )


def test_redirect_feed_carries_new_feed_url():
    xml = render_redirect_feed("Old Title", "https://x/new/audio_feed.xml", "https://x/new/")
    assert "<itunes:new-feed-url>https://x/new/audio_feed.xml</itunes:new-feed-url>" in xml


def test_redirect_page_has_canonical_and_refresh():
    html = render_redirect_page("https://x/new/")
    assert 'rel=canonical href="https://x/new/"' in html
    assert "http-equiv=refresh" in html


def test_write_aliases_emits_feeds_pages_and_manifest(tmp_path):
    cities = [_city(aliases=["denton-tx", "old-denton"])]
    feed_info = {"denton-tx-city-council": {"has_audio": True, "has_video": False}}
    _write_aliases(tmp_path, "https://site", cities, feed_info)

    new_feed = "https://site/denton-tx-city-council/audio_feed.xml"
    alias_feed = (tmp_path / "denton-tx" / "audio_feed.xml").read_text()
    assert f"<itunes:new-feed-url>{new_feed}</itunes:new-feed-url>" in alias_feed
    assert (tmp_path / "denton-tx" / "index.html").exists()
    assert not (tmp_path / "denton-tx" / "video_feed.xml").exists()  # no video for this feed

    manifest = json.loads((tmp_path / "redirects.json").read_text())
    froms = {r["from"] for r in manifest}
    assert "/denton-tx/" in froms and "/old-denton/audio_feed.xml" in froms
    assert all(r["to"].startswith("https://site/denton-tx-city-council/") for r in manifest)


def test_no_aliases_writes_empty_manifest(tmp_path):
    _write_aliases(tmp_path, "https://site", [_city()], {})
    assert json.loads((tmp_path / "redirects.json").read_text()) == []


# --- orphan GC ---------------------------------------------------------------------------


def test_referenced_audio_keys_scans_record_stores(tmp_path):
    save_records(
        tmp_path,
        "src1",
        {"u1": {"audio": {"key": "p/src1/u1-abc.m4a", "url": "x"}}, "u2": {"audio": {}}},
    )
    save_records(tmp_path, "src2", {"u3": {"audio": {"key": "p/src2/u3-def.m4a"}}})
    assert referenced_audio_keys(tmp_path) == {"p/src1/u1-abc.m4a", "p/src2/u3-def.m4a"}


def test_local_storage_list_and_delete(tmp_path):
    store = LocalStorage(root=tmp_path / "a", url_prefix="https://cdn")
    (tmp_path / "a" / "p").mkdir(parents=True)
    (tmp_path / "a" / "p" / "keep.m4a").write_bytes(b"1")
    (tmp_path / "a" / "p" / "orphan.m4a").write_bytes(b"2")
    keys = {k for k, _ in store.list_objects()}
    assert keys == {"p/keep.m4a", "p/orphan.m4a"}
    store.delete("p/orphan.m4a")
    assert {k for k, _ in store.list_objects()} == {"p/keep.m4a"}


def test_gc_keeps_durable_state_objects(tmp_path):
    """Orphan GC must never reap the state/ snapshot, even though it's unreferenced audio."""
    from scripts import gc_audio

    out = tmp_path / "docs"
    audio = out / "audio"
    (audio / "p").mkdir(parents=True)
    (audio / "p" / "orphan.m4a").write_bytes(b"x")
    (audio / "state" / "sources").mkdir(parents=True)
    (audio / "state" / "sources" / "ep.json").write_bytes(b"{}")

    state = tmp_path / "state"
    save_records(state, "src", {"u1": {"audio": {"key": "p/kept.m4a", "url": "x"}}})
    (out / "audio" / "p" / "kept.m4a").write_bytes(b"k")
    write_local_backend_site_config(tmp_path, state)
    argv = ["--site-config", str(tmp_path / "site.yml"), "--output-dir", str(out)]
    argv += ["--min-age-days", "0", "--apply"]
    assert gc_audio.main(argv) == 0
    assert not (audio / "p" / "orphan.m4a").exists()
    assert (audio / "state" / "sources" / "ep.json").exists()  # state preserved


def test_state_sync_round_trips_through_storage(tmp_path):
    from citypods.statesync import pull_state, push_state

    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    src = tmp_path / "state-a"
    save_records(src, "src1", {"u1": {"audio": {"key": "k", "url": "u"}}})
    (src / "feed_etags.json").write_text('{"slug": {"content_hash": "abc"}}')

    assert push_state(store, src) == 2

    dst = tmp_path / "state-b"  # simulate a fresh runner after cache eviction
    assert pull_state(store, dst) == 2
    from citypods.records import load_records

    assert load_records(dst, "src1") == {"u1": {"audio": {"key": "k", "url": "u"}}}
    assert (dst / "feed_etags.json").read_text() == '{"slug": {"content_hash": "abc"}}'


def test_prune_stale_dirs_removes_dropped_slugs(tmp_path):
    from citypods.run import _prune_stale_dirs

    out = tmp_path
    (out / "audio").mkdir()  # reserved: must survive
    (out / "audio" / "x.m4a").write_bytes(b"1")
    for slug in ("live-tx", "alias-tx", "dropped-tx"):
        (out / slug).mkdir()
        (out / slug / "audio_feed.xml").write_text("<rss/>")
    (out / "index.html").write_text("home")  # top-level file: untouched

    cities = [_city(slug="live-tx", aliases=["alias-tx"])]
    _prune_stale_dirs(out, cities)

    assert (out / "live-tx").exists()
    assert (out / "alias-tx").exists()
    assert not (out / "dropped-tx").exists()
    assert (out / "audio" / "x.m4a").exists()
    assert (out / "index.html").exists()


def test_gc_script_dry_run_then_apply(tmp_path):
    from scripts import gc_audio

    out = tmp_path / "docs"
    audio = out / "audio" / "p"
    audio.mkdir(parents=True)
    (audio / "kept.m4a").write_bytes(b"1")
    (audio / "orphan.m4a").write_bytes(b"2")

    state = tmp_path / "state"
    save_records(state, "src", {"u1": {"audio": {"key": "p/kept.m4a", "url": "x"}}})
    write_local_backend_site_config(tmp_path, state)
    argv = ["--site-config", str(tmp_path / "site.yml"), "--output-dir", str(out)]
    argv += ["--min-age-days", "0"]

    assert gc_audio.main(argv) == 0  # dry-run: nothing deleted
    assert (audio / "orphan.m4a").exists()
    assert gc_audio.main([*argv, "--apply"]) == 0
    assert not (audio / "orphan.m4a").exists()
    assert (audio / "kept.m4a").exists()
