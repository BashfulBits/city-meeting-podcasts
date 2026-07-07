"""Tests for the unified storage-reclaim policy (GH#496): lifecycle rule building + guardrail,
the append-only reclaim log, the double-confirm orphan ledger, and the resurrection watchdog."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from citypods.ops import reclaim, reclaim_log

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module's __dict__ for type annotations.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gc_audio = _load_script("gc_audio")
check_reclaim_resurrection = _load_script("check_reclaim_resurrection")


# ── reclaim: lifecycle rule building + guardrail ─────────────────────────────────────


def test_r2_scratch_rules_are_prefix_scoped_and_short_ttl():
    rules = reclaim.build_r2_scratch_rules()
    prefixes = {r["Filter"]["Prefix"] for r in rules}
    assert prefixes == set(reclaim.R2_SCRATCH_PREFIXES)
    assert all(r["Expiration"]["Days"] == reclaim.R2_SCRATCH_TTL_DAYS for r in rules)
    assert all(r["Status"] == "Enabled" for r in rules)
    reclaim.assert_r2_rules_scoped(rules)  # our own rules must pass the guardrail


def test_guardrail_rejects_a_broad_live_prefix():
    # A rule targeting the bare coordination prefix would expire LIVE leases mid-job.
    broad = [{"ID": "x", "Filter": {"Prefix": "work-leases/"}, "Status": "Enabled"}]
    with pytest.raises(AssertionError, match="not strictly inside"):
        reclaim.assert_r2_rules_scoped(broad)


def test_guardrail_rejects_bucket_wide_prefix():
    with pytest.raises(AssertionError):
        reclaim.assert_r2_rules_scoped([{"ID": "x", "Filter": {"Prefix": ""}, "Status": "Enabled"}])


def test_b2_retention_rule_keeps_noncurrent_versions():
    rules = reclaim.build_b2_retention_rules(retention_days=30)
    assert rules[0]["NoncurrentVersionExpiration"]["NoncurrentDays"] == 30
    assert rules[0]["Expiration"]["ExpiredObjectDeleteMarker"] is True


def test_merge_managed_rules_preserves_unmanaged_and_replaces_managed():
    existing = [
        {"ID": "user-abort-multipart", "Filter": {"Prefix": ""}},  # unmanaged: keep
        {"ID": "reclaim-old", "Filter": {"Prefix": "work-leases/__validate__/"}},  # managed: drop
    ]
    managed = reclaim.build_r2_scratch_rules()
    merged = reclaim.merge_managed_rules(existing, managed)
    ids = [r["ID"] for r in merged]
    assert "user-abort-multipart" in ids
    assert "reclaim-old" not in ids  # replaced by freshly-built managed rules
    assert all(r in merged for r in managed)


def test_config_getters_read_defaults():
    assert reclaim.b2_retention_days({"b2_retention_days": 14}) == 14
    assert reclaim.b2_retention_days({}) == reclaim.B2_RETENTION_DAYS_DEFAULT
    assert reclaim.orphan_quarantine_days({"orphan_quarantine_days": 5}) == 5.0
    assert reclaim.orphan_quarantine_days(None) == reclaim.ORPHAN_QUARANTINE_DAYS_DEFAULT


# ── reclaim_log: append/load, recover_by, prune-by-age ───────────────────────────────


def test_make_entry_recover_by_only_for_b2():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    b2 = reclaim_log.make_entry(
        "a.m4a", backend="b2", reason="orphan-auto", retention_days=30, now=now
    )
    r2 = reclaim_log.make_entry("work-leases/x", backend="r2", reason="scratch", now=now)
    assert b2.recover_by == (now + timedelta(days=30)).isoformat()
    assert r2.recover_by is None  # R2 has no version history — permanent, never "recoverable"


def test_append_and_load_roundtrip(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    entries = [
        reclaim_log.make_entry(
            "a.m4a", backend="b2", reason="orphan-auto", retention_days=30, now=now
        ),
        reclaim_log.make_entry(
            "b.m4a", backend="b2", reason="orphan-manual", retention_days=30, now=now
        ),
    ]
    reclaim_log.append_deletions(tmp_path, entries, retention_days=30, now=now)
    loaded = reclaim_log.load(tmp_path)
    assert [e.key for e in loaded] == ["a.m4a", "b.m4a"]
    # A second append accumulates (append-only).
    reclaim_log.append_deletions(
        tmp_path,
        [
            reclaim_log.make_entry(
                "c.m4a", backend="b2", reason="orphan-auto", retention_days=30, now=now
            )
        ],
        retention_days=30,
        now=now,
    )
    assert [e.key for e in reclaim_log.load(tmp_path)] == ["a.m4a", "b.m4a", "c.m4a"]


def test_load_recoverable_filters_by_deadline(tmp_path):
    now = datetime(2026, 6, 1, tzinfo=UTC)
    old = datetime(2026, 1, 1, tzinfo=UTC)  # recover_by = 2026-01-31, already past at `now`
    reclaim_log.append_deletions(
        tmp_path,
        [
            reclaim_log.make_entry(
                "live.m4a", backend="b2", reason="o", retention_days=30, now=now
            ),
            reclaim_log.make_entry(
                "expired.m4a", backend="b2", reason="o", retention_days=30, now=old
            ),
        ],
        retention_days=30,
        now=old,  # append at old time so the expired one isn't pruned yet
    )
    recoverable = {e.key for e in reclaim_log.load_recoverable(tmp_path, now=now)}
    assert recoverable == {"live.m4a"}  # expired.m4a is past its window


def test_prune_drops_entries_well_past_recovery(tmp_path):
    old = datetime(2026, 1, 1, tzinfo=UTC)
    reclaim_log.append_deletions(
        tmp_path,
        [reclaim_log.make_entry("stale.m4a", backend="b2", reason="o", retention_days=30, now=old)],
        retention_days=30,
        now=old,
    )
    # Much later append prunes the stale entry (recover_by + one extra retention window has passed).
    way_later = old + timedelta(days=200)
    reclaim_log.append_deletions(
        tmp_path,
        [
            reclaim_log.make_entry(
                "fresh.m4a", backend="b2", reason="o", retention_days=30, now=way_later
            )
        ],
        retention_days=30,
        now=way_later,
    )
    keys = {e.key for e in reclaim_log.load(tmp_path)}
    assert keys == {"fresh.m4a"}  # stale pruned, only recent audit history retained


def test_prune_keeps_r2_entries_for_bounded_audit_window(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reclaim_log.append_deletions(
        tmp_path,
        [reclaim_log.make_entry("work-leases/x", backend="r2", reason="scratch", now=now)],
        retention_days=30,
        now=now,
    )
    # Within 2× retention → kept; beyond → pruned.
    reclaim_log.append_deletions(tmp_path, [], retention_days=30, now=now + timedelta(days=40))
    assert {e.key for e in reclaim_log.load(tmp_path)} == {"work-leases/x"}
    reclaim_log.append_deletions(tmp_path, [], retention_days=30, now=now + timedelta(days=90))
    assert reclaim_log.load(tmp_path) == []


# ── gc_audio: double-confirm orphan ledger ───────────────────────────────────────────


def test_ledger_does_not_auto_delete_on_first_sighting():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    ledger: dict = {}
    auto = gc_audio._update_orphan_ledger(ledger, {"a.m4a"}, now=now, quarantine_days=21)
    assert auto == set()  # run_count==1, and no quarantine time elapsed
    assert ledger["a.m4a"]["run_count"] == 1


def test_ledger_auto_deletes_after_two_runs_past_quarantine():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    ledger: dict = {}
    gc_audio._update_orphan_ledger(ledger, {"a.m4a"}, now=first, quarantine_days=21)
    # Second sighting 22 days later: run_count==2 AND first_seen older than the 21d quarantine.
    later = first + timedelta(days=22)
    auto = gc_audio._update_orphan_ledger(ledger, {"a.m4a"}, now=later, quarantine_days=21)
    assert auto == {"a.m4a"}


def test_ledger_requires_quarantine_time_not_just_two_runs():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    ledger: dict = {}
    gc_audio._update_orphan_ledger(ledger, {"a.m4a"}, now=first, quarantine_days=21)
    # Second sighting only 1 day later: 2 runs, but quarantine window not elapsed → no auto-delete.
    auto = gc_audio._update_orphan_ledger(
        ledger, {"a.m4a"}, now=first + timedelta(days=1), quarantine_days=21
    )
    assert auto == set()


def test_ledger_flip_flop_drops_and_never_matures():
    first = datetime(2026, 1, 1, tzinfo=UTC)
    ledger: dict = {}
    gc_audio._update_orphan_ledger(ledger, {"a.m4a"}, now=first, quarantine_days=21)
    # Next run: a.m4a is no longer a candidate (re-referenced) → dropped from the ledger.
    gc_audio._update_orphan_ledger(
        ledger, set(), now=first + timedelta(days=10), quarantine_days=21
    )
    assert "a.m4a" not in ledger
    # It reappears as orphan much later — but its history reset, so it's a first sighting again.
    auto = gc_audio._update_orphan_ledger(
        ledger, {"a.m4a"}, now=first + timedelta(days=60), quarantine_days=21
    )
    assert auto == set()
    assert ledger["a.m4a"]["run_count"] == 1


# ── check_reclaim_resurrection: the recovery backstop ────────────────────────────────


def _write_episode(state_dir: Path, src: str, uid: str, audio_key: str) -> None:
    src_dir = state_dir / "sources" / src
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "episodes.json").write_text(
        json.dumps({"episodes": {uid: {"audio": {"key": audio_key}}}})
    )


def test_watchdog_flags_a_re_referenced_reaped_key(tmp_path):
    now = datetime(2026, 6, 1, tzinfo=UTC)
    key = "granicus/src1/uid1-spec.m4a"
    _write_episode(tmp_path, "src1", "uid1", key)  # a live record references it again
    reclaim_log.append_deletions(
        tmp_path,
        [
            reclaim_log.make_entry(
                key, backend="b2", reason="orphan-auto", retention_days=30, now=now
            )
        ],
        retention_days=30,
        now=now,
    )
    hits = check_reclaim_resurrection.find_resurrections(tmp_path, now=now)
    assert [h.key for h in hits] == [key]
    body = check_reclaim_resurrection.render_issue_body(hits)
    assert key in body and "restore before" in body.lower()


def test_watchdog_ignores_key_past_recovery_window(tmp_path):
    deleted = datetime(2026, 1, 1, tzinfo=UTC)  # recover_by 2026-01-31
    later = datetime(2026, 6, 1, tzinfo=UTC)  # window long closed
    key = "granicus/src1/uid1-spec.m4a"
    _write_episode(tmp_path, "src1", "uid1", key)
    reclaim_log.append_deletions(
        tmp_path,
        [reclaim_log.make_entry(key, backend="b2", reason="o", retention_days=30, now=deleted)],
        retention_days=30,
        now=deleted,
    )
    assert check_reclaim_resurrection.find_resurrections(tmp_path, now=later) == []


def test_watchdog_clean_when_no_reaped_key_is_referenced(tmp_path):
    now = datetime(2026, 6, 1, tzinfo=UTC)
    _write_episode(tmp_path, "src1", "uid1", "granicus/src1/uid1-spec.m4a")
    reclaim_log.append_deletions(
        tmp_path,
        [
            reclaim_log.make_entry(
                "granicus/src1/OTHER-x.m4a", backend="b2", reason="o", retention_days=30, now=now
            )
        ],
        retention_days=30,
        now=now,
    )
    assert check_reclaim_resurrection.find_resurrections(tmp_path, now=now) == []


def test_watchdog_ignores_r2_deletes(tmp_path):
    # R2 deletes have no recover_by (never recoverable), so even if somehow referenced they aren't
    # flagged — they were unrecoverable at delete time, which the R2-ephemeral invariant guards.
    now = datetime(2026, 6, 1, tzinfo=UTC)
    key = "work-leases/src1/uid1.json"
    _write_episode(tmp_path, "src1", "uid1", key)
    reclaim_log.append_deletions(
        tmp_path,
        [reclaim_log.make_entry(key, backend="r2", reason="scratch", now=now)],
        retention_days=30,
        now=now,
    )
    assert check_reclaim_resurrection.find_resurrections(tmp_path, now=now) == []


# ── apply_bucket_lifecycle: idempotence via a fake backend ───────────────────────────


class _FakeLifecycleBackend:
    def __init__(self):
        self._rules: list[dict] = []
        self.puts = 0

    def get_lifecycle_rules(self):
        return list(self._rules)

    def put_lifecycle_rules(self, rules):
        self._rules = list(rules)
        self.puts += 1


def test_apply_lifecycle_reconcile_is_idempotent():
    apply_bucket_lifecycle = _load_script("apply_bucket_lifecycle")
    backend = _FakeLifecycleBackend()
    rules = reclaim.build_r2_scratch_rules()

    assert apply_bucket_lifecycle._reconcile(backend, rules, label="r2", apply=True) is True
    assert backend.puts == 1  # first apply writes
    assert apply_bucket_lifecycle._reconcile(backend, rules, label="r2", apply=True) is True
    assert backend.puts == 1  # already up to date → no second PUT


def test_apply_lifecycle_dry_run_never_writes():
    apply_bucket_lifecycle = _load_script("apply_bucket_lifecycle")
    backend = _FakeLifecycleBackend()
    rules = reclaim.build_r2_scratch_rules()
    assert apply_bucket_lifecycle._reconcile(backend, rules, label="r2", apply=False) is True
    assert backend.puts == 0


# ── gc_audio: report body reflects auto-reaped counts ────────────────────────────────


def test_issue_body_reflects_auto_reaped_count():
    # In --auto-confirm mode matured orphans WERE deleted even though args.apply is False, so the
    # persisted issue body must not read as a pure dry-run (GH#496 CodeRabbit finding #6).
    summary = {
        "by_city": {"Austin": {"files": 2, "bytes": 2048}},
        "total_files": 2,
        "total_bytes": 2048,
    }
    body = gc_audio.render_issue_body(summary, applied=False, auto_reaped=3)
    assert "3 object(s) were auto-reaped" in body
    assert "remain" in body.lower()
    assert "dry-run" not in body.lower()  # not a plain dry-run when objects were auto-reaped


def test_issue_body_plain_dry_run_when_nothing_reaped():
    summary = {
        "by_city": {"Austin": {"files": 1, "bytes": 1024}},
        "total_files": 1,
        "total_bytes": 1024,
    }
    body = gc_audio.render_issue_body(summary, applied=False, auto_reaped=0)
    assert "dry-run" in body.lower()
    assert "auto-reaped" not in body.lower()


# ── gc_audio: reclaim log is committed per-key BEFORE each delete ─────────────────────


class _FakeGcStorage:
    """Minimal storage double: lists a fixed object set, records deletes, and can be told to raise
    on a specific key to simulate a mid-loop kill."""

    name = "b2"

    def __init__(self, objects, *, fail_on=None):
        self._objects = objects  # list[(key, last_modified, size)]
        self.deleted: list[str] = []
        self._fail_on = fail_on

    def iter_objects(self, prefix):
        for key, lm, size in self._objects:
            if key.startswith(prefix):
                yield key, lm, size

    def delete(self, key):
        if key == self._fail_on:
            raise RuntimeError(f"simulated kill deleting {key}")
        self.deleted.append(key)


def _run_gc_main(monkeypatch, tmp_path, storage, argv):
    """Drive gc_audio.main() against a fake storage, stubbing config/record lookups."""
    monkeypatch.setattr(gc_audio, "make_storage", lambda *a, **k: storage)
    monkeypatch.setattr(gc_audio, "load_site_config", lambda *a, **k: {"defaults": {}})
    monkeypatch.setattr(gc_audio, "load_city_configs", lambda *a, **k: [])
    monkeypatch.setattr(gc_audio, "resolve_state_dir", lambda *a, **k: tmp_path)
    # A non-empty live set so the "refuse to GC" guard passes; none of the candidates are in it.
    monkeypatch.setattr(gc_audio, "referenced_audio_keys", lambda *a, **k: {"live/keep.m4a"})
    return gc_audio.main(argv)


def test_reclaim_log_written_before_each_delete_survives_midloop_kill(monkeypatch, tmp_path):
    # Three matured orphans; the storage raises while deleting the second. Because the reclaim-log
    # entry is committed BEFORE each delete, the log must already contain the key that was
    # successfully deleted first — no silent miss (GH#496 CodeRabbit finding #5, the MAJOR one).
    old = (datetime.now(UTC) - timedelta(days=400)).replace(microsecond=0)
    objects = [
        ("granicus/src1/a-spec.m4a", old, 10),
        ("granicus/src1/b-spec.m4a", old, 20),
        ("granicus/src1/c-spec.m4a", old, 30),
    ]
    storage = _FakeGcStorage(objects, fail_on="granicus/src1/b-spec.m4a")

    with pytest.raises(RuntimeError, match="simulated kill"):
        _run_gc_main(
            monkeypatch,
            tmp_path,
            storage,
            ["--apply", "--min-age-days", "0"],
        )

    # First key was deleted; the crash hit the second before it was removed.
    assert storage.deleted == ["granicus/src1/a-spec.m4a"]
    logged = {e.key for e in reclaim_log.load(tmp_path)}
    # The successfully-deleted key has a durable record (would be caught by the watchdog).
    assert "granicus/src1/a-spec.m4a" in logged
    # The crashing key was logged just before its (failed) delete — a harmless phantom entry, the
    # deliberately-safer failure mode; the third key was never reached.
    assert "granicus/src1/c-spec.m4a" not in logged


def test_reclaim_log_records_all_keys_on_clean_apply(monkeypatch, tmp_path):
    old = (datetime.now(UTC) - timedelta(days=400)).replace(microsecond=0)
    objects = [
        ("granicus/src1/a-spec.m4a", old, 10),
        ("granicus/src1/b-spec.m4a", old, 20),
    ]
    storage = _FakeGcStorage(objects)
    rc = _run_gc_main(monkeypatch, tmp_path, storage, ["--apply", "--min-age-days", "0"])
    assert rc == 0
    assert set(storage.deleted) == {"granicus/src1/a-spec.m4a", "granicus/src1/b-spec.m4a"}
    assert {e.key for e in reclaim_log.load(tmp_path)} == {
        "granicus/src1/a-spec.m4a",
        "granicus/src1/b-spec.m4a",
    }
