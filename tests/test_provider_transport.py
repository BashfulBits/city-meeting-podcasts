"""Per-tenant Granicus transport telemetry (H16 transport criterion)."""

from __future__ import annotations

from citypods.provider_transport import ProviderTransportTelemetry

FORT_WORTH = (
    "https://archive-video.granicus.com/fortworthgov/"
    "fortworthgov_e4cc067f-6b2d-11f1-9494-005056a89546.mp4"
)
DENTON = (
    "https://archive-video.granicus.com/dentoncounty/"
    "dentoncounty_220e6b97-9746-49ef-8718-f46efbbf7e0c.mp4"
)
FORT_KEY = "granicus.com/tenant:fortworthgov"
DENTON_KEY = "granicus.com/tenant:dentoncounty"


def test_record_worker_fallback_counts_per_tenant_and_ignores_non_granicus():
    """GH#337 evaluation telemetry: per-tenant attempt/success/failure counts, in-memory only."""
    telemetry = ProviderTransportTelemetry(["granicus.com"])

    telemetry.record_worker_fallback([FORT_WORTH], outcome="success")
    telemetry.record_worker_fallback([FORT_WORTH], outcome="failure")
    telemetry.record_worker_fallback([DENTON], outcome="success")
    # A non-Granicus URL has no configured scope and is ignored, like the other record_* methods.
    telemetry.record_worker_fallback(["https://example.com/whatever.mp4"], outcome="success")

    counts = telemetry.telemetry()
    assert set(counts) == {FORT_KEY, DENTON_KEY}
    assert counts[FORT_KEY]["worker_fallback_attempts"] == 2
    assert counts[FORT_KEY]["worker_fallback_successes"] == 1
    assert counts[FORT_KEY]["worker_fallback_failures"] == 1
    assert counts[DENTON_KEY]["worker_fallback_attempts"] == 1
    assert counts[DENTON_KEY]["worker_fallback_successes"] == 1


def test_record_direct_fetch_and_truncation_telemetry():
    telemetry = ProviderTransportTelemetry(["granicus.com"])

    telemetry.record_direct_fetch([FORT_WORTH], outcome="success")
    telemetry.record_direct_fetch([FORT_WORTH], outcome="403")
    telemetry.record_truncation([FORT_WORTH])
    telemetry.record_direct_fetch(["https://example.com/video.mp4"], outcome="success")

    counts = telemetry.telemetry()
    assert set(counts) == {FORT_KEY}
    assert counts[FORT_KEY]["direct_fetch_successes"] == 1
    assert counts[FORT_KEY]["direct_fetch_403s"] == 1
    assert counts[FORT_KEY]["truncations"] == 1


def test_scope_identity_uses_archive_tenant_and_tenant_subdomain():
    telemetry = ProviderTransportTelemetry(["granicus.com"])

    telemetry.record_direct_fetch([FORT_WORTH], outcome="success")
    telemetry.record_direct_fetch(
        ["https://fortworthgov.granicus.com/DownloadFile.php?clip_id=1"], outcome="success"
    )
    telemetry.record_direct_fetch(
        ["https://swagit-video.granicus.com/archive/2012/08/07/video.mp4"], outcome="success"
    )

    counts = telemetry.telemetry()
    # The archive-video path tenant and the tenant subdomain both resolve to the same scope.
    assert counts[FORT_KEY]["direct_fetch_successes"] == 2
    assert "granicus.com/tenant:swagit-video" in counts


def test_no_configured_domains_records_nothing():
    telemetry = ProviderTransportTelemetry([])
    assert not telemetry.has_domains()
    telemetry.record_direct_fetch([FORT_WORTH], outcome="success")
    assert telemetry.telemetry() == {}


def test_legacy_mapping_config_shape_is_accepted():
    # The old config shape (domain -> circuit rule) is still accepted; only the keys matter now.
    telemetry = ProviderTransportTelemetry({"granicus.com": {"threshold": 3}})
    assert telemetry.has_domains()
    telemetry.record_truncation([FORT_WORTH])
    assert telemetry.telemetry()[FORT_KEY]["truncations"] == 1


def test_invalid_direct_fetch_outcome_rejected():
    telemetry = ProviderTransportTelemetry(["granicus.com"])
    try:
        telemetry.record_direct_fetch([FORT_WORTH], outcome="nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unsupported outcome")


def test_invalid_worker_fallback_outcome_rejected():
    telemetry = ProviderTransportTelemetry(["granicus.com"])
    try:
        telemetry.record_worker_fallback([FORT_WORTH], outcome="nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unsupported worker fallback outcome")
