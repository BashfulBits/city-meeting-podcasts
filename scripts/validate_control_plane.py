#!/usr/bin/env python
"""Live validation of the H17 R2/CAS control plane (review/17 + review/18).

Exercises the *real* plumbing — ``RoutingStorage`` routing, native R2 compare-and-swap
(``put_cas``/``get_bytes``), the Stage-2 work-lease ledger (``claim``/``renew``/``release``/
``reap``), and the provider concurrency-slot pool (``DistributedProviderLeasePool``) — against
**live B2 + R2** from a runner, so you can confirm credentials, routing, and CAS semantics work
end-to-end **before** any production data flows through the new pipeline (i.e. before / right after
flipping ``audio_storage_backend`` to ``routing``).

**It never touches production data.** All objects are written under unique scratch namespaces —
``work-leases/__validate__/<run-id>/…`` and ``provider-leases/__validate__-<run-id>…/…`` (both
coordination prefixes, so the router sends them to R2) and deleted on exit. The production discovery
index never references ``__validate__``, so ``reconcile`` never sweeps these, and the real
budget/lease/slot keys are not read or written.

Required env (same as ``r2_from_env`` + ``b2_from_env``):
  CLOUDFLARE_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL
  B2_ENDPOINT, B2_KEY_ID, B2_APP_KEY, B2_BUCKET, B2_PUBLIC_BASE_URL
Optional: R2_ENDPOINT (jurisdiction-specific buckets).

Exit code 0 iff every check passes. See .github/workflows/validate-control-plane.yml.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta

from citypods.http import StopRequested
from citypods.ops import work_leases as wl
from citypods.storage import CASConflict


def _check(results: list[dict], name: str, ok: bool, detail: str = "") -> None:
    results.append({"check": name, "pass": bool(ok), "detail": detail})


def validate(storage, *, run_id: str, now: datetime | None = None) -> dict:
    """Run the control-plane checks against ``storage`` (a CAS-capable backend). Pure + reusable:
    the offline test drives it with an in-memory CAS fake; the CLI drives it with live routing.
    Cleans up every scratch object it creates."""
    now = now or datetime.now(UTC)
    results: list[dict] = []
    src = f"__validate__/{run_id}"  # scratch source namespace under work-leases/
    created: list[str] = []

    # 1) Routing + capability: the coordination backend must advertise real CAS, and a coordination
    #    key must route to it while a blob key (audio/) stays on the B2 primary. ``cas_capable`` is
    #    a property/attr on our backends; a validator still shouldn't false-green if one ever
    #    exposes it as a method — so call it if callable.
    cap_attr = getattr(storage, "cas_capable", False)
    cas_capable = bool(cap_attr() if callable(cap_attr) else cap_attr)
    _check(results, "cas_capable", cas_capable, f"cas_capable={cas_capable}")
    is_managed = getattr(storage, "is_cas_managed_key", None)
    if callable(is_managed):
        routed = is_managed(wl.lease_key(src, "probe")) and not is_managed("audio/x.m4a")
        _check(
            results,
            "routing_namespaced",
            routed,
            "work-leases→R2, audio→B2" if routed else "routing misconfigured",
        )

    # 2) Native CAS primitives on R2: create-if-absent, conditional update, stale rejection.
    probe = f"work-leases/__validate__/{run_id}/cas-probe.json"
    created.append(probe)
    try:
        _url, etag1 = storage.put_cas(probe, b'{"v":1}', "application/json", if_none_match="*")
        got = storage.get_bytes(probe)
        roundtrip = got is not None and got[0] == b'{"v":1}' and got[1] == etag1
        _check(results, "cas_create_if_absent", roundtrip, f"etag={etag1}")
        try:
            storage.put_cas(probe, b'{"v":1}', "application/json", if_none_match="*")
            _check(results, "cas_create_conflict", False, "expected CASConflict on existing key")
        except CASConflict:
            _check(results, "cas_create_conflict", True, "re-create rejected (412)")
        _url, etag2 = storage.put_cas(probe, b'{"v":2}', "application/json", if_match=etag1)
        _check(results, "cas_update_if_match", etag2 != etag1, f"etag {etag1}→{etag2}")
        try:
            storage.put_cas(probe, b'{"v":3}', "application/json", if_match=etag1)  # stale
            _check(results, "cas_update_stale", False, "expected CASConflict on stale ETag")
        except CASConflict:
            _check(results, "cas_update_stale", True, "stale update rejected (412)")
    except Exception as exc:  # noqa: BLE001 — report, don't crash the run
        _check(results, "cas_primitives", False, f"unexpected error: {exc!r}")

    # 3) Work-lease ledger: claim → contend → renew → release(done), and the reaper requeue path.
    try:
        created.append(wl.lease_key(src, "u1"))
        held = wl.claim(
            storage,
            src,
            "u1",
            owner="validator-A",
            ttl_seconds=300,
            pipeline_version="validate",
            now=now,
        )
        _check(results, "lease_claim", held is not None and held.state == "leased", "claimed u1")
        contended = wl.claim(storage, src, "u1", owner="validator-B", ttl_seconds=300, now=now)
        _check(results, "lease_contended_skips", contended is None, "second worker blocked")
        renewed = wl.renew(storage, src, "u1", owner="validator-A", ttl_seconds=600, now=now)
        _check(results, "lease_renew", renewed is not None, "holder renewed")
        released = wl.release(storage, src, "u1", owner="validator-A", state="done", now=now)
        _check(results, "lease_release", released, "released → done")

        created.append(wl.lease_key(src, "u2"))
        wl.claim(
            storage, src, "u2", owner="validator-dead", ttl_seconds=1, now=now - timedelta(hours=1)
        )  # already expired
        reaped = wl.reap(storage, [(src, "u2")], artifact_present=lambda s, u: False, now=now)
        _check(results, "lease_reaper_requeues_expired", reaped["requeued"] == 1, str(reaped))
    except Exception as exc:  # noqa: BLE001
        _check(results, "lease_ledger", False, f"unexpected error: {exc!r}")

    # 4) Provider concurrency slots: drive the real pool end-to-end on R2. Two pools fill a 2-slot
    #    domain (independent slot objects / ETags), a third caller cannot acquire while both are
    #    held, and releasing frees the slot — proof of the per-slot CAS protocol over live routing.
    from citypods.provider_leases import DistributedProviderLeasePool

    lease_domain = f"validate-{run_id}.example".lower()
    lease_url = f"https://{lease_domain}/probe"
    lease_cfg = {lease_domain: {"slots": 2, "ttl_seconds": 3600, "poll_seconds": 0.01}}
    pool_a = DistributedProviderLeasePool()
    pool_b = DistributedProviderLeasePool()
    pool_c = DistributedProviderLeasePool()
    for pool in (pool_a, pool_b, pool_c):
        pool.configure(storage, lease_cfg)
    created.append(pool_a._slot_key(lease_domain, 0))
    created.append(pool_a._slot_key(lease_domain, 1))

    def _held_slots() -> int:
        return sum(storage.get_bytes(pool_a._slot_key(lease_domain, i)) is not None for i in (0, 1))

    try:
        # held_a stays held across the cap check; held_b is released first to observe the staged
        # held→1→0 transition. try/finally guarantees both are released even if a check throws, so
        # a failure never leaves a live holder (whose renewal thread keeps re-claiming) behind for
        # cleanup to race.
        held_a = pool_a.slots([lease_url])
        held_a.__enter__()
        try:
            held_b = pool_b.slots([lease_url])
            held_b.__enter__()
            try:
                _check(
                    results,
                    "provider_slots_acquire",
                    _held_slots() == 2,
                    f"{_held_slots()}/2 slots held",
                )

                # A third caller, given a stop budget that fires after one full sweep, must not
                # acquire while both slots are held — proof the distributed cap holds across pools.
                sweeps = {"n": 0}

                def _stop_after_one_sweep() -> bool:
                    sweeps["n"] += 1
                    return sweeps["n"] > 1

                try:
                    with pool_c.slots([lease_url], stop=_stop_after_one_sweep):
                        _check(
                            results,
                            "provider_slots_cap_blocks",
                            False,
                            "third caller wrongly acquired",
                        )
                except StopRequested:
                    _check(
                        results,
                        "provider_slots_cap_blocks",
                        True,
                        "third caller blocked while full",
                    )
            finally:
                held_b.__exit__(None, None, None)
            after_one = _held_slots()  # one released → exactly one slot object remains
        finally:
            held_a.__exit__(None, None, None)
        after_both = _held_slots()  # both released → no slot objects remain
        _check(
            results,
            "provider_slots_release",
            after_one == 1 and after_both == 0,
            f"held→{after_one}→{after_both} as slots release",
        )
    except Exception as exc:  # noqa: BLE001
        _check(results, "provider_slots", False, f"unexpected error: {exc!r}")

    # Cleanup: delete every scratch object we created (best-effort; never fail the run on cleanup).
    cleaned = 0
    for key in created:
        try:
            storage.delete(key)
            cleaned += 1
        except Exception:  # noqa: BLE001
            pass

    overall = all(r["pass"] for r in results)
    report = {
        "run_id": run_id,
        "overall_pass": overall,
        "checks": results,
        "scratch_cleaned": cleaned,
        "telemetry": storage.telemetry() if hasattr(storage, "telemetry") else {},
    }
    return report


def _make_routing_storage():
    from citypods.storage import RoutingStorage
    from citypods.storage.s3 import b2_from_env, r2_from_env

    primary = b2_from_env()
    coordination = r2_from_env()
    if primary is None:
        raise SystemExit("missing B2_* env (the primary backend); cannot validate")
    if coordination is None:
        raise SystemExit("missing R2_*/CLOUDFLARE_ACCOUNT_ID env (the coordination backend)")
    return RoutingStorage(primary=primary, coordination=coordination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="write the JSON report to this path")
    parser.add_argument(
        "--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    args = parser.parse_args(argv)

    storage = _make_routing_storage()
    report = validate(storage, run_id=args.run_id)
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(text + "\n")
    return 0 if report["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
