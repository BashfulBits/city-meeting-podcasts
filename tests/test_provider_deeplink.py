"""Tests for INFRA-6 (issue #147): provider deep-link capability.

Acceptance criteria:
- Per-provider unit tests of the generated URL.
- Capability gating (only providers with "deeplink" in capabilities produce URLs).
- Protocol structural conformance.
- (Liveness / endpoint-contract test in tests/live/ — separate, not run by default.)
"""

from __future__ import annotations

from citypods.providers.civicclerk import CivicClerkProvider
from citypods.providers.civicplus import CivicPlusProvider
from citypods.providers.granicus import GranicusProvider
from citypods.providers.swagit import SwagitProvider

# ---------------------------------------------------------------------------
# GranicusProvider — video_deeplink
# ---------------------------------------------------------------------------


class TestGranicusDeeplink:
    PLAYER_URL = "https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=5455"

    def test_capabilities_includes_deeplink(self):
        assert "deeplink" in GranicusProvider.capabilities

    def test_deeplink_appends_starttime(self):
        p = GranicusProvider()
        url = p.video_deeplink(self.PLAYER_URL, 300)
        assert url == f"{self.PLAYER_URL}&starttime=300"

    def test_deeplink_truncates_fractional_seconds(self):
        p = GranicusProvider()
        url = p.video_deeplink(self.PLAYER_URL, 412.7)
        assert url is not None
        assert "&starttime=412" in url

    def test_deeplink_zero_seconds(self):
        p = GranicusProvider()
        url = p.video_deeplink(self.PLAYER_URL, 0)
        assert url is not None
        assert "starttime=0" in url

    def test_deeplink_returns_none_for_non_player_url(self):
        p = GranicusProvider()
        # A direct MP4 URL (not a MediaPlayer page) should return None safely
        assert p.video_deeplink("https://cdn.granicus.com/meeting.mp4", 60) is None

    def test_deeplink_preserves_existing_params(self):
        p = GranicusProvider()
        url = p.video_deeplink(self.PLAYER_URL, 100)
        assert "view_id=2" in url
        assert "clip_id=5455" in url
        assert "starttime=100" in url

    def test_deeplink_for_different_tenant(self):
        ref = "https://dentontx.granicus.com/MediaPlayer.php?view_id=7&clip_id=999"
        url = GranicusProvider().video_deeplink(ref, 60)
        assert url == f"{ref}&starttime=60"


# ---------------------------------------------------------------------------
# SwagitProvider — video_deeplink
# ---------------------------------------------------------------------------


class TestSwagitDeeplink:
    WATCH_URL = "https://dallastx.new.swagit.com/videos/389304"

    def test_capabilities_includes_deeplink(self):
        assert "deeplink" in SwagitProvider.capabilities

    def test_deeplink_uses_play_path(self):
        p = SwagitProvider()
        url = p.video_deeplink(self.WATCH_URL, 491)
        assert url == "https://dallastx.new.swagit.com/play/389304/491"

    def test_deeplink_truncates_fractional_seconds(self):
        p = SwagitProvider()
        url = p.video_deeplink(self.WATCH_URL, 51.9)
        assert url is not None
        assert "/play/389304/51" in url

    def test_deeplink_zero_seconds(self):
        p = SwagitProvider()
        url = p.video_deeplink(self.WATCH_URL, 0)
        assert url is not None
        assert "/play/389304/0" in url

    def test_deeplink_returns_none_for_unparseable_ref(self):
        p = SwagitProvider()
        # A download URL (not a canonical /videos/{id} page)
        bad_url = "https://dallastx.new.swagit.com/videos/389304/download"
        assert p.video_deeplink(bad_url, 60) is None

    def test_deeplink_returns_none_for_non_numeric_id(self):
        p = SwagitProvider()
        assert p.video_deeplink("https://swagit.com/videos/abc", 60) is None

    def test_deeplink_preserves_origin(self):
        ref = "https://fortworth.swagit.com/videos/12345"
        url = SwagitProvider().video_deeplink(ref, 200)
        assert url == "https://fortworth.swagit.com/play/12345/200"

    def test_deeplink_chapter_anchor_pattern_matches(self):
        """Verify the generated URL matches the pattern seen in Swagit chapter anchors."""
        # Live pages show: href="/play/389304/51" — our deeplink produces the absolute version
        ref = "https://dallastx.new.swagit.com/videos/389304"
        url = SwagitProvider().video_deeplink(ref, 51)
        assert url is not None
        # Strip to path and verify it matches the chapter anchor pattern
        from urllib.parse import urlsplit

        path = urlsplit(url).path
        assert path == "/play/389304/51"


# ---------------------------------------------------------------------------
# CivicClerkProvider — no deeplink support
# ---------------------------------------------------------------------------


class TestCivicClerkDeeplink:
    def test_capabilities_excludes_deeplink(self):
        assert "deeplink" not in CivicClerkProvider.capabilities

    def test_video_deeplink_returns_none(self):
        p = CivicClerkProvider()
        result = p.video_deeplink("https://cdn.civicclerk.com/meeting.mp4", 120)
        assert result is None


# ---------------------------------------------------------------------------
# CivicPlusProvider — no deeplink support
# ---------------------------------------------------------------------------


class TestCivicPlusDeeplink:
    def test_capabilities_excludes_deeplink(self):
        assert "deeplink" not in CivicPlusProvider.capabilities

    def test_video_deeplink_returns_none(self):
        p = CivicPlusProvider()
        result = p.video_deeplink("https://src/hls.m3u8", 60)
        assert result is None


# ---------------------------------------------------------------------------
# Capability gating — only "deeplink" providers produce URLs
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    def test_providers_with_deeplink_produce_non_none(self):
        """Every provider with "deeplink" in capabilities must return a non-None URL
        for a valid ref."""
        cases = [
            (
                GranicusProvider(),
                "https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=5455",
            ),
            (SwagitProvider(), "https://dallastx.new.swagit.com/videos/389304"),
        ]
        for provider, ref in cases:
            assert "deeplink" in provider.capabilities
            url = provider.video_deeplink(ref, 60)
            assert url is not None, f"{provider.name} returned None for a valid ref"
            assert url.startswith("http"), f"{provider.name} URL doesn't start with http"

    def test_providers_without_deeplink_return_none(self):
        """Every provider without "deeplink" must return None from video_deeplink."""
        cases = [
            (CivicClerkProvider(), "https://cdn.civicclerk.com/meeting.mp4"),
            (CivicPlusProvider(), "https://src/hls.m3u8"),
        ]
        for provider, ref in cases:
            assert "deeplink" not in provider.capabilities
            assert provider.video_deeplink(ref, 60) is None

    def test_all_providers_have_capabilities_attribute(self):
        """Every provider must declare capabilities (even if empty)."""
        providers = [
            GranicusProvider(),
            SwagitProvider(),
            CivicClerkProvider(),
            CivicPlusProvider(),
        ]
        for p in providers:
            assert hasattr(p, "capabilities"), f"{p.name} missing capabilities"
            assert isinstance(p.capabilities, frozenset), f"{p.name} capabilities is not frozenset"

    def test_all_providers_have_video_deeplink_method(self):
        """Every provider must implement video_deeplink (returns None when unsupported)."""
        providers = [
            GranicusProvider(),
            SwagitProvider(),
            CivicClerkProvider(),
            CivicPlusProvider(),
        ]
        for p in providers:
            assert hasattr(p, "video_deeplink"), f"{p.name} missing video_deeplink"
            assert callable(p.video_deeplink), f"{p.name}.video_deeplink is not callable"


# ---------------------------------------------------------------------------
# URL structure assertions
# ---------------------------------------------------------------------------


class TestDeeplinkUrlStructure:
    def test_granicus_url_is_valid_https(self):
        url = GranicusProvider().video_deeplink(
            "https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=5455", 300
        )
        assert url is not None
        assert url.startswith("https://")
        assert "?" in url  # has query params

    def test_swagit_url_is_valid_https(self):
        url = SwagitProvider().video_deeplink("https://dallastx.new.swagit.com/videos/389304", 491)
        assert url is not None
        assert url.startswith("https://")
        assert "/play/" in url

    def test_granicus_and_swagit_urls_differ_in_format(self):
        g_url = GranicusProvider().video_deeplink(
            "https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=5455", 100
        )
        s_url = SwagitProvider().video_deeplink(
            "https://dallastx.new.swagit.com/videos/389304", 100
        )
        # Granicus uses query param; Swagit uses path segment
        assert "&starttime=" in g_url  # type: ignore[operator]
        assert "/play/" in s_url  # type: ignore[operator]
