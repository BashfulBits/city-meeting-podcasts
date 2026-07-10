"""Tests for scripts/refresh_fixtures.py (MR-SC-05: one city's failure must not abort the run)."""

from __future__ import annotations

from citypods.models import City
from scripts import refresh_fixtures


def _city(slug, feed_url="https://x.granicus.com/feed.xml"):
    return City(
        slug=slug,
        provider="granicus",
        source={"feed_url": feed_url},
        podcast_title="T",
        podcast_author="City of T",
        podcast_email="",
        podcast_description="d",
    )


class _FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, *, fail_slugs):
        self._fail_slugs = fail_slugs
        self.requested = []

    def get(self, url, params=None, timeout=None):
        self.requested.append(url)
        if "bad" in url:
            raise ConnectionError("simulated network failure")
        return _FakeResp(b"<rss></rss>")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_one_citys_fetch_failure_does_not_abort_the_whole_run(monkeypatch, tmp_path):
    cities = [_city("bad-city", "https://bad.granicus.com/feed.xml"), _city("good-city")]
    monkeypatch.setattr(refresh_fixtures, "load_site_config", lambda *a, **k: {})
    monkeypatch.setattr(refresh_fixtures, "load_city_configs", lambda *a, **k: cities)
    session = _FakeSession(fail_slugs={"bad-city"})
    monkeypatch.setattr(refresh_fixtures, "make_session", lambda: session)
    monkeypatch.setattr(refresh_fixtures, "ROOT", tmp_path)
    monkeypatch.setattr(refresh_fixtures, "FIXTURE_DIR", tmp_path / "fixtures")

    refresh_fixtures.main()  # must not raise

    # The good city's fixture was still written despite the bad city's failure.
    assert (tmp_path / "fixtures" / "granicus" / "good-city.xml").exists()
    assert not (tmp_path / "fixtures" / "granicus" / "bad-city.xml").exists()
