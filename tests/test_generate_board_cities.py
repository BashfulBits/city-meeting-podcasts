"""Tests for scripts/generate_board_cities.py (CR2-SC-06/07)."""

from __future__ import annotations

from datetime import UTC, datetime

from citypods.models import City, Episode
from scripts import generate_board_cities as gbc


def _city(slug, body=None, extra_source=None):
    source = {"feed_url": "https://x.granicus.com/feed.xml"}
    if body is not None:
        source["body"] = body
    if extra_source:
        source.update(extra_source)
    return City(
        slug=slug,
        provider="granicus",
        source=source,
        podcast_title="T",
        podcast_author="City of T",
        podcast_email="",
        podcast_description="d",
    )


def _episode(guid, body):
    return Episode(
        guid=guid,
        title=f"{body} meeting",
        published=datetime.now(UTC),
        video_url=f"https://x/{guid}.mp4",
        body=body,
    )


class _FakeProvider:
    def __init__(self, episodes):
        self._episodes = episodes

    def fetch_episodes(self, source):
        return self._episodes


def _run(monkeypatch, *, template, cities, episodes, argv):
    monkeypatch.setattr(gbc, "load_site_config", lambda *a, **k: {"defaults": {}})
    monkeypatch.setattr(gbc, "load_city_configs", lambda *a, **k: cities)
    monkeypatch.setattr(gbc, "get_provider", lambda name: _FakeProvider(episodes))
    return gbc.main([template, *argv])


def test_covered_set_normalizes_stored_body_before_matching(tmp_path, capsys, monkeypatch):
    # CR2-SC-06: a stored source["body"] carrying the raw "on <datetime>..." suffix must still be
    # recognized as covering the same body key the discovery side computes via canonical() — else
    # this would (incorrectly) plan a duplicate feed for a body an existing feed already covers.
    template = _city("base-tx")
    existing = _city("base-tx-council", body="City Council on 2026-05-19 4:00 PM")
    episodes = [_episode(f"g{i}", "City Council") for i in range(5)]

    rc = _run(
        monkeypatch,
        template="base-tx",
        cities=[template, existing],
        episodes=episodes,
        argv=[
            "--base-slug",
            "base-tx",
            "--title-prefix",
            "Base",
            "--config-dir",
            str(tmp_path),
            "--site-config",
            str(tmp_path / "site_config.yml"),
        ],
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "0 feeds" in out
    assert "base-tx-city-council" not in out


def test_same_run_slug_collision_is_flagged_not_silently_skipped(tmp_path, capsys, monkeypatch):
    # CR2-SC-07: two distinct bodies that slugify() collapses to the same slug must be flagged as
    # a same-run collision, not logged as "skip (exists)" as if it were a stale pre-existing file
    # (which would silently discard a distinct board feed). "Fire & Rescue" and "Fire Rescue" are
    # genuinely different body_key() values (body_key expands "&" to "and") but slugify() strips
    # "&" as plain punctuation, so both collapse to "fire-rescue".
    template = _city("base-tx")
    episodes = [_episode(f"g{i}", "Fire & Rescue") for i in range(5)] + [
        _episode(f"h{i}", "Fire Rescue") for i in range(5)
    ]

    rc = _run(
        monkeypatch,
        template="base-tx",
        cities=[template],
        episodes=episodes,
        argv=[
            "--base-slug",
            "base-tx",
            "--title-prefix",
            "Base",
            "--config-dir",
            str(tmp_path),
            "--site-config",
            str(tmp_path / "site_config.yml"),
        ],
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "slug collision this run" in out
    assert "skip (exists)" not in out
