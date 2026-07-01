"""Tests for the H14a external-dispatch substrate: free-tier budget ledger, routing, live
``work.json`` leases, and the run-start ``compute reconcile``.

A :class:`FakeDispatchBackend` stands in for the real Modal/Beam adapters (H14b/H14c) so the whole
machinery — dispatch records a lease + decrements budget, budget-exhausted falls back to ``local``,
reconcile reaps an expired lease, the result-write contract round-trips an artifact onto a mock
bucket, and the monthly cap is never exceeded — is exercised without a real serverless GPU.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.compute import (
    DispatchCoordinator,
    DispatchTarget,
    lease_owner_for,
    reconcile_compute,
    select_backend,
)
from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.budget import (
    BUDGET_STATE_KEY,
    Budget,
    load_budget,
    load_budget_cas,
    month_key,
    mutate_budget,
    reserve_if_available,
    save_budget,
)
from citypods.ops.workqueue import WorkItem, is_leased, lease, load_manifest, save_manifest
from citypods.storage import CASConflict
from citypods.storage.local import LocalStorage

NOW = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
# Reconcile's work-lease sweep is a no-op on a non-CAS backend (no ledger) → zeroed sub-summary.
_NO_LEASES = {"completed": 0, "requeued": 0, "in_flight": 0}


# ── fakes ──────────────────────────────────────────────────────────────────────


class FakeDispatchBackend:
    """A dispatch backend that submits (returns a JobHandle) without running anything, plus a
    ``write_result`` that plays the remote worker writing the content-addressed artifact back."""

    def __init__(self, name: str = "modal", est: float = 90.0):
        self.name = name
        self._est = est
        self._n = 0
        self.submitted: list[InferenceJob] = []

    def estimate_gpu_seconds(self, job: InferenceJob) -> float:
        return self._est

    def run_inference(self, job: InferenceJob) -> JobHandle:
        self._n += 1
        self.submitted.append(job)
        return JobHandle(
            task=job.task, recipe_hash=job.recipe_hash, backend=self.name, ref=f"job-{self._n}"
        )

    @staticmethod
    def write_result(
        storage,
        item: WorkItem,
        recipe: str,
        *,
        vtt: bytes = b"WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n",
        words: bytes = b'{"words": []}',
        observed_seconds: float | None = None,
    ) -> None:
        """Simulate the worker: write the content-addressed ``.vtt`` + ``.words.json`` to the bucket
        and mark the manifest *item* done (recording actual GPU seconds if known)."""
        base = f"transcripts/{item.source_key}/{item.episode_uid}-asr-{recipe}"
        for suffix, data, ctype in (
            (".vtt", vtt, "text/vtt"),
            (".words.json", words, "application/json"),
        ):
            with tempfile.NamedTemporaryFile() as tf:
                tf.write(data)
                tf.flush()
                storage.put_file(base + suffix, Path(tf.name), ctype)
        item.state = "done"
        if observed_seconds is not None:
            item.observed_seconds = observed_seconds


class _LocalStub:
    """Minimal synchronous backend: the overflow target. ``try_dispatch`` never invokes it (it
    returns ``None`` to hand the work back to the caller), so it only needs to exist + be named."""

    name = "local"

    def run_inference(self, job: InferenceJob) -> JobResult:
        return JobResult(task=job.task, recipe_hash=job.recipe_hash, output=b"local")


def _job(task: str = "transcribe", recipe: str = "r1") -> InferenceJob:
    return InferenceJob(task=task, inputs={"audio_url": "https://x/a.m4a"}, recipe_hash=recipe)


def _item(uid: str = "u1", work_class: str = "transcript-asr") -> WorkItem:
    return WorkItem(source_key="s1", episode_uid=uid, work_class=work_class, city_slug="c")


class _MemBucket:
    """In-memory bucket exposing BOTH the bulk surface (put_file/get_file/list_objects) and the CAS
    surface (get_bytes/put_cas), so it stands in for an R2-backed routing storage. ``cas_capable``
    is True; ``put_cas`` enforces If-Match/If-None-Match like R2."""

    cas_capable = True
    name = "r2"

    def __init__(self):
        self.objs: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self._n = 0

    def _bump(self, key: str) -> str:
        self._n += 1
        self.etags[key] = f'"e{self._n}"'
        return self.etags[key]

    # bulk surface
    def put_file(self, key, local_path, content_type):
        self.objs[key] = Path(local_path).read_bytes()
        self._bump(key)
        return "mem://" + key

    def get_file(self, key, local_path):
        if key not in self.objs:
            return False
        p = Path(local_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.objs[key])
        return True

    def list_objects(self, prefix=""):
        for k in sorted(self.objs):
            if k.startswith(prefix):
                yield k, None

    def exists(self, key):
        return key in self.objs

    def delete(self, key):
        self.objs.pop(key, None)
        self.etags.pop(key, None)

    def public_url(self, key):
        return "mem://" + key

    # CAS surface
    def get_bytes(self, key):
        if key not in self.objs:
            return None
        return self.objs[key], self.etags[key]

    def put_cas(self, key, data, content_type, *, if_none_match=None, if_match=None):
        from citypods.storage import CASConflict

        exists = key in self.objs
        if if_none_match == "*" and exists:
            raise CASConflict(key)
        if if_match is not None and (not exists or self.etags.get(key) != if_match):
            raise CASConflict(key)
        self.objs[key] = data
        return "mem://" + key, self._bump(key)


def _coordinator(state_dir, *, fake=None, cap=10_000.0, max_inflight=8, budget=None, storage=None):
    targets = (
        [DispatchTarget(backend=fake, monthly_gpu_seconds=cap, max_inflight=max_inflight)]
        if fake is not None
        else []
    )
    return DispatchCoordinator(
        local=_LocalStub(),
        targets=targets,
        budget=budget or Budget(month=month_key(NOW)),
        state_dir=state_dir,
        storage=storage,
        lease_ttl_seconds=3600,
    )


# ── budget ledger ───────────────────────────────────────────────────────────────


class TestBudget:
    def test_available_gates_on_cap_and_slots(self):
        b = Budget()
        assert b.available("modal", cap=100, max_inflight=2, est=90)
        b.reserve("modal:1", "modal", 90)
        # 90 used + 90 est = 180 > 100 → no budget for another 90.
        assert not b.available("modal", cap=100, max_inflight=2, est=90)
        # A tiny job still fits the cap, and there's an open slot (1 of 2 used).
        assert b.available("modal", cap=100, max_inflight=2, est=5)

    def test_max_inflight_blocks_when_slots_full(self):
        b = Budget()
        b.reserve("modal:1", "modal", 1)
        b.reserve("modal:2", "modal", 1)
        assert not b.available("modal", cap=1_000, max_inflight=2, est=1)

    def test_settle_swaps_estimate_for_actual(self):
        b = Budget()
        b.reserve("modal:1", "modal", 90)
        b.settle("modal:1", "modal", 70)  # actual < estimate
        led = b.backends["modal"]
        assert led.used_gpu_seconds == 70 and led.inflight_count == 0

    def test_settle_without_actual_keeps_estimate_charged(self):
        # A completed job with no actuals channel must NOT return budget (else the cap could be
        # overspent): the estimate stays charged, only the in-flight slot frees.
        b = Budget()
        b.reserve("modal:1", "modal", 90)
        b.settle("modal:1", "modal")
        led = b.backends["modal"]
        assert led.used_gpu_seconds == 90 and led.inflight_count == 0

    def test_release_returns_reservation(self):
        b = Budget()
        b.reserve("modal:1", "modal", 90)
        b.release("modal:1", "modal")
        led = b.backends["modal"]
        assert led.used_gpu_seconds == 0 and led.inflight_count == 0

    def test_roll_month_resets(self):
        b = Budget(month="2026-05")
        b.reserve("modal:1", "modal", 90)
        assert b.roll_month(NOW) is True
        assert b.month == month_key(NOW)
        assert b.backends == {}
        assert b.roll_month(NOW) is False  # same month → no-op

    def test_persistence_round_trips(self, tmp_path):
        b = Budget(month="2026-06")
        b.reserve("modal:1", "modal", 42.5)
        save_budget(tmp_path, b)
        loaded = load_budget(tmp_path)
        assert loaded.month == "2026-06"
        assert loaded.backends["modal"].used_gpu_seconds == 42.5
        assert loaded.backends["modal"].inflight == {"modal:1": 42.5}

    def test_load_missing_is_empty(self, tmp_path):
        assert load_budget(tmp_path).backends == {}

    def test_cap_never_exceeded_over_a_month(self):
        # The $0 guarantee: drive dispatches until the backend is spent; ``used`` must never cross
        # the cap, and once spent the backend refuses more.
        b = Budget()
        cap, est = 1000.0, 90.0
        granted = 0
        for i in range(100):
            if b.available("modal", cap=cap, max_inflight=999, est=est):
                b.reserve(f"modal:{i}", "modal", est)
                granted += 1
            # ``available`` creates the ledger, so it exists from the first iteration on.
            assert b.backends["modal"].used_gpu_seconds <= cap
        assert granted == int(cap // est)  # 11 jobs × 90 = 990 ≤ 1000; the 12th is refused


# ── routing ─────────────────────────────────────────────────────────────────────


class TestSelectBackend:
    def test_fills_free_tier_first(self):
        fake = FakeDispatchBackend("modal", est=90)
        target = DispatchTarget(fake, monthly_gpu_seconds=1000, max_inflight=8)
        backend, chosen = select_backend(
            _job(), targets=[target], local=_LocalStub(), budget=Budget(), now=NOW
        )
        assert backend is fake and chosen is target

    def test_overflows_to_local_when_exhausted(self):
        fake = FakeDispatchBackend("modal", est=90)
        target = DispatchTarget(fake, monthly_gpu_seconds=50, max_inflight=8)  # cap < est
        local = _LocalStub()
        backend, chosen = select_backend(
            _job(), targets=[target], local=local, budget=Budget(), now=NOW
        )
        assert backend is local and chosen is None

    def test_prefers_first_target_then_second(self):
        modal = FakeDispatchBackend("modal", est=90)
        beam = FakeDispatchBackend("beam", est=90)
        budget = Budget()
        budget.reserve("modal:full", "modal", 1000)  # modal spent
        targets = [
            DispatchTarget(modal, monthly_gpu_seconds=1000, max_inflight=8),
            DispatchTarget(beam, monthly_gpu_seconds=1000, max_inflight=8),
        ]
        backend, chosen = select_backend(
            _job(), targets=targets, local=_LocalStub(), budget=budget, now=NOW
        )
        assert backend is beam and chosen.backend is beam


# ── coordinator ─────────────────────────────────────────────────────────────────


class TestDispatchCoordinator:
    def test_dispatch_records_lease_and_decrements_budget(self, tmp_path):
        fake = FakeDispatchBackend("modal", est=90)
        coord = _coordinator(tmp_path, fake=fake)
        item = _item()
        handle = coord.try_dispatch(item, _job(), now=NOW)

        assert isinstance(handle, JobHandle)
        owner = lease_owner_for(handle)
        assert owner == f"modal:{handle.ref}"
        # The work item now holds the live lease.
        assert item.lease_owner == owner and item.state == "running"
        assert is_leased(item, now=NOW)
        # Budget decremented + persisted eagerly.
        persisted = load_budget(tmp_path)
        assert persisted.backends["modal"].used_gpu_seconds == 90
        assert owner in persisted.backends["modal"].inflight

    def test_budget_exhausted_falls_back_to_local(self, tmp_path):
        fake = FakeDispatchBackend("modal", est=90)
        coord = _coordinator(tmp_path, fake=fake, cap=50)  # cap < est ⇒ never affordable
        assert coord.try_dispatch(_item(), _job(), now=NOW) is None  # overflow to local
        assert fake.submitted == []  # never even submitted

    def test_no_dispatch_backend_overflows_to_local(self, tmp_path):
        coord = _coordinator(tmp_path)  # no targets (the H14a production reality)
        assert coord.dispatch_enabled is False
        assert coord.try_dispatch(_item(), _job(), now=NOW) is None

    def test_in_flight_guard(self, tmp_path):
        fake = FakeDispatchBackend("modal", est=90)
        coord = _coordinator(tmp_path, fake=fake)
        item = _item()
        coord.try_dispatch(item, _job(), now=NOW)
        assert coord.is_inflight("s1", "u1", "transcript-asr", now=NOW)
        # A different work_class for the same episode is independent.
        assert not coord.is_inflight("s1", "u1", "transcript-align", now=NOW)
        # The lease expires.
        assert not coord.is_inflight("s1", "u1", "transcript-asr", now=NOW + timedelta(hours=2))

    def test_seed_leases_adopts_unexpired_only(self, tmp_path):
        coord = _coordinator(tmp_path)
        live = _item("live")
        lease(live, "modal:a", ttl_seconds=3600, now=NOW)
        dead = _item("dead")
        lease(dead, "modal:b", ttl_seconds=3600, now=NOW - timedelta(hours=2))  # already expired
        coord.seed_leases([live, dead], now=NOW)
        assert coord.is_inflight("s1", "live", "transcript-asr", now=NOW)
        assert not coord.is_inflight("s1", "dead", "transcript-asr", now=NOW)

    def test_live_leases_for_manifest_overlay(self, tmp_path):
        fake = FakeDispatchBackend("modal", est=90)
        coord = _coordinator(tmp_path, fake=fake)
        item = _item()
        handle = coord.try_dispatch(item, _job(), now=NOW)
        leases = coord.live_leases(now=NOW)
        assert leases[("s1", "u1", "transcript-asr")][0] == lease_owner_for(handle)

    def test_run_inference_safety_net_delegates_to_local(self, tmp_path):
        coord = _coordinator(tmp_path)
        result = coord.run_inference(_job())
        assert isinstance(result, JobResult) and result.output == b"local"


# ── reconcile ───────────────────────────────────────────────────────────────────


class TestReconcile:
    def test_reaps_expired_lease_and_requeues(self, tmp_path):
        item = _item()
        lease(item, "modal:job-1", ttl_seconds=3600, now=NOW - timedelta(hours=2))  # expired
        save_manifest(tmp_path, [item])
        budget = Budget(month=month_key(NOW))
        budget.reserve("modal:job-1", "modal", 90)
        save_budget(tmp_path, budget)

        summary = reconcile_compute(tmp_path, storage=None, now=NOW)

        assert summary == {"reaped": 1, "settled": 0, "in_flight": 0, "leases": _NO_LEASES}
        requeued = load_manifest(tmp_path)[0]
        assert requeued.state == "queued" and requeued.lease_owner == ""
        # Dead worker's reservation returned to the pool.
        assert load_budget(tmp_path).backends["modal"].inflight == {}
        assert load_budget(tmp_path).backends["modal"].used_gpu_seconds == 0

    def test_settles_completed_job_when_artifact_present(self, tmp_path):
        bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
        item = _item()
        lease(item, "modal:job-1", ttl_seconds=3600, now=NOW)  # still valid
        item.observed_seconds = 70.0
        FakeDispatchBackend.write_result(bucket, item, "r1", observed_seconds=70.0)
        save_manifest(tmp_path, [item])
        budget = Budget(month=month_key(NOW))
        budget.reserve("modal:job-1", "modal", 90)
        save_budget(tmp_path, budget)

        summary = reconcile_compute(tmp_path, bucket, now=NOW)

        assert summary == {"reaped": 0, "settled": 1, "in_flight": 0, "leases": _NO_LEASES}
        done = load_manifest(tmp_path)[0]
        assert done.state == "done" and done.lease_owner == ""
        led = load_budget(tmp_path).backends["modal"]
        assert led.used_gpu_seconds == 70 and led.inflight == {}  # actuals reconciled

    def test_leaves_running_lease_alone(self, tmp_path):
        item = _item()
        lease(item, "modal:job-1", ttl_seconds=3600, now=NOW)  # valid, no artifact yet
        save_manifest(tmp_path, [item])
        save_budget(tmp_path, Budget(month=month_key(NOW)))
        summary = reconcile_compute(tmp_path, storage=None, now=NOW)
        assert summary == {"reaped": 0, "settled": 0, "in_flight": 1, "leases": _NO_LEASES}
        assert load_manifest(tmp_path)[0].state == "running"

    def test_work_lease_reaper_dormant_by_default_leaves_expired_claim_untouched(self, tmp_path):
        # The Stage-2 ledger is dormant until external pull workers exist, so reconcile must NOT
        # sweep it by default — even on a CAS backend with an expired claim and a pending item.
        from citypods.ops import work_leases as wl

        bucket = _MemBucket()
        item = _item("u1")  # pending transcript-asr (state defaults to "queued")
        save_manifest(tmp_path, [item])
        wl.claim(  # a dead worker's expired claim sitting in the ledger
            bucket, "s1", "u1", owner="dead", ttl_seconds=1, now=NOW - timedelta(hours=1)
        )
        before = bucket.objs[wl.lease_key("s1", "u1")]

        summary = reconcile_compute(tmp_path, bucket, now=NOW)  # sweep_work_leases defaults False

        assert summary["leases"] == _NO_LEASES  # ledger not swept
        assert bucket.objs[wl.lease_key("s1", "u1")] == before  # the expired claim is untouched

    def test_work_lease_reaper_requeues_expired_claim_when_enabled(self, tmp_path):
        from citypods.ops import work_leases as wl

        bucket = _MemBucket()
        item = _item("u1")
        save_manifest(tmp_path, [item])
        wl.claim(bucket, "s1", "u1", owner="dead", ttl_seconds=1, now=NOW - timedelta(hours=1))

        summary = reconcile_compute(tmp_path, bucket, now=NOW, sweep_work_leases=True)

        assert summary["leases"]["requeued"] == 1  # dead worker's claim reclaimed
        reclaimed, _etag = wl.read_lease(bucket, "s1", "u1")
        assert reclaimed.state == "queued" and reclaimed.owner == ""

    def test_empty_manifest_is_noop(self, tmp_path):
        assert reconcile_compute(tmp_path, storage=None, now=NOW) == {
            "reaped": 0,
            "settled": 0,
            "in_flight": 0,
            "leases": _NO_LEASES,
        }


# ── result-write contract ───────────────────────────────────────────────────────


class TestResultWriteContract:
    def test_artifact_round_trips_onto_mock_bucket(self, tmp_path):
        bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
        item = _item()
        FakeDispatchBackend.write_result(
            bucket, item, "rX", vtt=b"WEBVTT\nROUND-TRIP\n", words=b'{"w": 1}'
        )

        vtt_key = f"transcripts/{item.source_key}/{item.episode_uid}-asr-rX.vtt"
        words_key = f"transcripts/{item.source_key}/{item.episode_uid}-asr-rX.words.json"
        assert bucket.exists(vtt_key) and bucket.exists(words_key)

        dest = tmp_path / "rt.vtt"
        assert bucket.get_file(vtt_key, dest)
        assert dest.read_bytes() == b"WEBVTT\nROUND-TRIP\n"

    def test_full_dispatch_then_reconcile_round_trip(self, tmp_path):
        # End-to-end: dispatch (lease + decrement), worker writes the artifact, reconcile settles.
        bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
        fake = FakeDispatchBackend("modal", est=90)
        coord = _coordinator(tmp_path, fake=fake)
        item = _item()
        handle = coord.try_dispatch(item, _job(), now=NOW)
        assert handle is not None
        # The worker completes and writes back; persist the now-done item for reconcile to find.
        FakeDispatchBackend.write_result(bucket, item, handle.recipe_hash, observed_seconds=60.0)
        save_manifest(tmp_path, [item])

        summary = reconcile_compute(tmp_path, bucket, now=NOW)
        assert summary["settled"] == 1
        assert load_budget(tmp_path).backends["modal"].used_gpu_seconds == 60
        assert load_manifest(tmp_path)[0].state == "done"


# ── CAS-backed budget ledger (H17 PR3) ────────────────────────────────────────────


class TestBudgetCAS:
    def test_load_budget_cas_absent_then_present(self):
        bucket = _MemBucket()
        budget, etag = load_budget_cas(bucket)
        assert etag is None and budget.backends == {}
        mutate_budget(bucket, lambda b: b.reserve("modal:1", "modal", 90.0))
        budget, etag = load_budget_cas(bucket)
        assert etag is not None
        assert budget.backends["modal"].used_gpu_seconds == 90.0
        # The durable object is the CAS key (so RoutingStorage sends it to R2).
        assert BUDGET_STATE_KEY in bucket.objs

    def test_load_budget_cas_treats_malformed_schema_as_empty(self):
        # Valid JSON, invalid schema (backends is a string) must not crash dispatch/reconcile —
        # treat as empty and keep the ETag so the next CAS write cleanly replaces it.
        bucket = _MemBucket()
        bucket.objs[BUDGET_STATE_KEY] = b'{"month": "2026-06", "backends": "oops"}'
        bucket.etags[BUDGET_STATE_KEY] = '"e1"'
        budget, etag = load_budget_cas(bucket)
        assert budget.backends == {} and etag == '"e1"'

    def test_mutate_budget_first_write_uses_if_none_match(self):
        bucket = _MemBucket()
        calls = []
        real = bucket.put_cas

        def spy(key, data, ct, *, if_none_match=None, if_match=None):
            calls.append((if_none_match, if_match))
            return real(key, data, ct, if_none_match=if_none_match, if_match=if_match)

        bucket.put_cas = spy
        mutate_budget(bucket, lambda b: b.reserve("modal:1", "modal", 10.0))  # create
        mutate_budget(bucket, lambda b: b.reserve("modal:2", "modal", 10.0))  # update
        assert calls[0] == ("*", None)  # first write: create-if-absent
        assert calls[1][0] is None and calls[1][1] is not None  # then If-Match on the etag

    def test_mutate_budget_retries_and_merges_sibling_reservation(self):
        # The overspend/lost-update guard: while we hold a stale snapshot, a sibling shard commits
        # its own reservation first; our conditional write must fail, reload, and re-apply onto the
        # fresh ledger so BOTH reservations survive (review/17 §3/§5).
        bucket = _MemBucket()
        mutate_budget(bucket, lambda b: None)  # seed an empty ledger (so an ETag exists)
        real = bucket.put_cas
        fired = {"n": 0}

        def flaky(key, data, ct, *, if_none_match=None, if_match=None):
            fired["n"] += 1
            if fired["n"] == 1:
                # a sibling reserves first, via the real CAS path → bumps the ETag
                b, etag = load_budget_cas(bucket)
                b.reserve("modal:sibling", "modal", 50.0)
                real(key, _serialize_budget(b), "application/json", if_match=etag)
                raise CASConflict(key)
            return real(key, data, ct, if_none_match=if_none_match, if_match=if_match)

        bucket.put_cas = flaky
        result = mutate_budget(
            bucket, lambda b: b.reserve("modal:me", "modal", 90.0), sleep=lambda _s: None
        )
        led = result.backends["modal"]
        assert set(led.inflight) == {"modal:sibling", "modal:me"}  # no lost update
        assert led.used_gpu_seconds == 140.0

    def test_reserve_if_available_refuses_on_retry_after_sibling_takes_last_slot(self):
        # The real overspend race: two shards select the same backend from a stale (empty) snapshot,
        # both try to reserve. cap fits ONE 90s job. The loser's CAS conflicts, and on retry it
        # MUST re-check availability against the fresh ledger and refuse — not blindly merge to
        # 180/100. (mutate_budget alone would merge; reserve_if_available re-checks.)
        bucket = _MemBucket()
        mutate_budget(bucket, lambda b: None, now=NOW)  # seed an empty ledger (ETag exists)
        real = bucket.put_cas
        fired = {"n": 0}

        def flaky(key, data, ct, *, if_none_match=None, if_match=None):
            fired["n"] += 1
            if fired["n"] == 1:  # a sibling grabs the only slot first, between our read and write
                b, etag = load_budget_cas(bucket)
                b.reserve("modal:sibling", "modal", 90.0)
                real(key, _serialize_budget(b), "application/json", if_match=etag)
                raise CASConflict(key)
            return real(key, data, ct, if_none_match=if_none_match, if_match=if_match)

        bucket.put_cas = flaky
        ok = reserve_if_available(
            bucket,
            "modal:me",
            "modal",
            est=90.0,
            cap=100.0,
            max_inflight=8,
            now=NOW,
            sleep=lambda _s: None,
        )
        assert ok is False  # re-check on retry saw 90 used → refused (overflow to local)
        led = load_budget_cas(bucket)[0].backends["modal"]
        assert led.used_gpu_seconds == 90.0  # only the sibling — cap not breached
        assert set(led.inflight) == {"modal:sibling"}

    def test_mutate_budget_raises_after_max_attempts(self):
        bucket = _MemBucket()
        mutate_budget(bucket, lambda b: None)

        def always_conflict(key, data, ct, *, if_none_match=None, if_match=None):
            raise CASConflict(key)

        bucket.put_cas = always_conflict
        try:
            mutate_budget(
                bucket,
                lambda b: b.reserve("x", "modal", 1.0),
                max_attempts=3,
                sleep=lambda _s: None,
            )
            raise AssertionError("expected CASConflict")
        except CASConflict:
            pass


def _serialize_budget(budget: Budget) -> bytes:
    from citypods.compute.budget import _serialize

    return _serialize(budget).encode()


class TestCoordinatorCAS:
    def test_cross_shard_dispatch_does_not_overspend(self, tmp_path):
        # Two shards share one durable ledger on R2. cap fits ONE 90s job. The second shard reads
        # the first's reservation (fresh load) and overflows to local instead of overspending.
        bucket = _MemBucket()
        coord_a = _coordinator(
            tmp_path / "a", fake=FakeDispatchBackend("modal"), cap=100.0, storage=bucket
        )
        coord_b = _coordinator(
            tmp_path / "b", fake=FakeDispatchBackend("modal"), cap=100.0, storage=bucket
        )
        assert coord_a.try_dispatch(_item("u1"), _job(), now=NOW) is not None  # reserved 90
        assert coord_b.try_dispatch(_item("u2"), _job(), now=NOW) is None  # 90+90>100 → local
        led = load_budget_cas(bucket)[0].backends["modal"]
        assert led.used_gpu_seconds == 90.0  # only one reservation committed
        assert led.inflight_count == 1

    def test_dispatch_persists_via_cas_not_local_file(self, tmp_path):
        bucket = _MemBucket()
        coord = _coordinator(
            tmp_path, fake=FakeDispatchBackend("modal"), cap=1000.0, storage=bucket
        )
        assert coord.try_dispatch(_item("u1"), _job(), now=NOW) is not None
        # Durable ledger is the R2 CAS object; no local fallback file is written on the CAS path.
        assert BUDGET_STATE_KEY in bucket.objs
        assert not (tmp_path / "compute_budget.json").exists()

    def test_reconcile_settles_via_cas(self, tmp_path):
        bucket = _MemBucket()
        fake = FakeDispatchBackend("modal", est=90.0)
        coord = _coordinator(tmp_path, fake=fake, cap=1000.0, storage=bucket)
        item = _item("u1")
        coord.try_dispatch(item, _job(), now=NOW)
        save_manifest(tmp_path, [item])  # the live lease rides the manifest overlay
        # worker finishes: artifact lands, actuals reported
        FakeDispatchBackend.write_result(bucket, item, "r1", observed_seconds=60.0)
        save_manifest(tmp_path, [item])
        summary = reconcile_compute(tmp_path, bucket, now=NOW)
        assert summary["settled"] == 1
        led = load_budget_cas(bucket)[0].backends["modal"]
        assert led.inflight_count == 0  # slot freed
        assert led.used_gpu_seconds == 60.0  # estimate swapped for the actual
