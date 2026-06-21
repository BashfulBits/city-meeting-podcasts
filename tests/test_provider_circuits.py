"""Cross-shard and tenant-isolation tests for provider throttle circuits."""

from __future__ import annotations

import threading
import time

from citypods.provider_circuits import MediaRateLimitCircuitBreaker
from citypods.storage.local import LocalStorage

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


def _circuit(
    store: LocalStorage,
    *,
    threshold: int = 3,
    cooldown: float = 60,
    emergency_tenants: int = 2,
    shard: str = "0/4",
) -> MediaRateLimitCircuitBreaker:
    return MediaRateLimitCircuitBreaker(
        {
            "granicus.com": {
                "threshold": threshold,
                "cooldown_seconds": cooldown,
                "emergency_tenants": emergency_tenants,
                "probe_ttl_seconds": 1,
            }
        },
        storage=store,
        shard=shard,
        prefix="test-provider-circuits",
    )


def test_record_worker_fallback_counts_per_tenant_and_ignores_non_granicus():
    """GH#337 evaluation telemetry: per-tenant attempt/success/failure counts, in-memory only."""
    breaker = MediaRateLimitCircuitBreaker({"granicus.com": {"threshold": 3}})

    breaker.record_worker_fallback([FORT_WORTH], outcome="success")
    breaker.record_worker_fallback([FORT_WORTH], outcome="failure")
    breaker.record_worker_fallback([DENTON], outcome="success")
    # A non-Granicus URL has no configured scope and is ignored, like the other record_* methods.
    breaker.record_worker_fallback(["https://example.com/whatever.mp4"], outcome="success")

    telemetry = breaker.telemetry()
    assert set(telemetry) == {FORT_KEY, DENTON_KEY}
    assert telemetry[FORT_KEY]["worker_fallback_attempts"] == 2
    assert telemetry[FORT_KEY]["worker_fallback_successes"] == 1
    assert telemetry[FORT_KEY]["worker_fallback_failures"] == 1
    assert telemetry[DENTON_KEY]["worker_fallback_attempts"] == 1
    assert telemetry[DENTON_KEY]["worker_fallback_successes"] == 1
    # A Worker success must not look like a throttle on the breaker.
    assert telemetry[FORT_KEY]["rate_limited"] == 0


def test_record_h16_direct_fetch_and_truncation_telemetry():
    breaker = MediaRateLimitCircuitBreaker({"granicus.com": {"threshold": 3}})

    breaker.record_direct_fetch([FORT_WORTH], outcome="success")
    breaker.record_direct_fetch([FORT_WORTH], outcome="403")
    breaker.record_truncation([FORT_WORTH])
    breaker.record_direct_fetch(["https://example.com/video.mp4"], outcome="success")

    telemetry = breaker.telemetry()
    assert set(telemetry) == {FORT_KEY}
    assert telemetry[FORT_KEY]["direct_fetch_successes"] == 1
    assert telemetry[FORT_KEY]["direct_fetch_403s"] == 1
    assert telemetry[FORT_KEY]["truncations"] == 1


def test_failures_from_multiple_shards_trip_one_shared_tenant_circuit(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    shards = [_circuit(store, shard=f"{n}/4") for n in range(3)]

    assert shards[0].record_rate_limited([FORT_WORTH]) is None
    assert shards[1].record_rate_limited([FORT_WORTH]) is None
    assert shards[2].record_rate_limited([FORT_WORTH]) == FORT_KEY

    assert shards[0].open_for([FORT_WORTH], refresh=True) == FORT_KEY
    assert shards[1].open_for([FORT_WORTH], refresh=True) == FORT_KEY
    assert (
        sum(circuit.telemetry().get(FORT_KEY, {}).get("circuit_trips", 0) for circuit in shards)
        == 1
    )


def test_one_tenant_does_not_suppress_another_tenant(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    first = _circuit(store, threshold=1, emergency_tenants=2, shard="0/4")
    sibling = _circuit(store, threshold=1, emergency_tenants=2, shard="1/4")

    assert first.record_rate_limited([FORT_WORTH]) == FORT_KEY
    assert sibling.open_for([FORT_WORTH], refresh=True) == FORT_KEY
    assert sibling.open_for([DENTON], refresh=True) is None


def test_two_tenant_trips_open_domain_emergency_circuit(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    first = _circuit(store, threshold=1, emergency_tenants=2, shard="0/4")
    second = _circuit(store, threshold=1, emergency_tenants=2, shard="1/4")

    assert first.record_rate_limited([FORT_WORTH]) == FORT_KEY
    assert second.record_rate_limited([DENTON]) == "granicus.com"

    assert first.open_for([FORT_WORTH], refresh=True) == "granicus.com"
    assert first.open_for([DENTON], refresh=True) == "granicus.com"
    assert second.telemetry()["granicus.com"]["circuit_trips"] == 1


def test_only_one_shard_claims_canary_and_sibling_observes_recovery(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    claimant = _circuit(store, threshold=1, cooldown=0.05, shard="0/4")
    sibling = _circuit(store, threshold=1, cooldown=0.05, shard="1/4")
    assert claimant.record_rate_limited([FORT_WORTH]) == FORT_KEY

    claimed = claimant.wait_for_recovery_probe(keys=[FORT_KEY], sleep=time.sleep, poll_seconds=0.01)
    assert claimed == FORT_KEY

    observed: list[str | None] = []

    def _wait_for_other_shard():
        observed.append(
            sibling.wait_for_recovery_probe(keys=[FORT_KEY], sleep=time.sleep, poll_seconds=0.01)
        )

    thread = threading.Thread(target=_wait_for_other_shard)
    thread.start()
    time.sleep(0.05)
    assert observed == []

    claimant.record_success([FORT_WORTH])
    thread.join(timeout=2)

    assert observed == [FORT_KEY]
    assert claimant.recovery_succeeded(FORT_KEY)
    assert sibling.recovery_succeeded(FORT_KEY)
    assert claimant.telemetry()[FORT_KEY]["recovery_probes"] == 1
    assert sibling.telemetry().get(FORT_KEY, {}).get("recovery_probes", 0) == 0


def test_boundary_refresh_discards_stale_open_cache_after_sibling_recovers(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    claimant = _circuit(store, threshold=1, cooldown=0.01, shard="0/4")
    sibling = _circuit(store, threshold=1, cooldown=0.01, shard="1/4")
    assert claimant.record_rate_limited([FORT_WORTH]) == FORT_KEY
    assert sibling.open_for([FORT_WORTH], refresh=True) == FORT_KEY

    assert (
        claimant.wait_for_recovery_probe(keys=[FORT_KEY], sleep=time.sleep, poll_seconds=0.005)
        == FORT_KEY
    )
    claimant.record_success([FORT_WORTH])

    assert sibling.open_for([FORT_WORTH], refresh=True) is None
    assert sibling.open_for([FORT_WORTH]) is None


def test_domain_emergency_canary_uses_previously_parked_tenant_item(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    first = _circuit(store, threshold=1, cooldown=0.2, shard="0/4")
    second = _circuit(store, threshold=1, cooldown=0.2, shard="1/4")
    assert first.record_rate_limited([FORT_WORTH]) == FORT_KEY
    assert second.record_rate_limited([DENTON]) == "granicus.com"

    assert (
        first.wait_for_recovery_probe(keys=[FORT_KEY], sleep=time.sleep, poll_seconds=0.005)
        == FORT_KEY
    )
    assert first.open_for([FORT_WORTH], refresh=True) is None

    assert first.record_rate_limited([FORT_WORTH]) == "granicus.com"
    assert not first.recovery_succeeded(FORT_KEY)
    assert first.telemetry()["granicus.com"]["recovery_probes"] == 1


def test_scope_identity_uses_archive_tenant_and_tenant_subdomain(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    circuit = _circuit(store)

    assert circuit.scope_keys_for([FORT_WORTH]) == [FORT_KEY]
    assert circuit.scope_keys_for(
        ["https://fortworthgov.granicus.com/DownloadFile.php?clip_id=1"]
    ) == [FORT_KEY]
    assert circuit.scope_keys_for(
        ["https://swagit-video.granicus.com/archive/2012/08/07/video.mp4"]
    ) == ["granicus.com/tenant:swagit-video"]
