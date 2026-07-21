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


def test_r2_lifecycle_rules_use_cloudflare_age_seconds_and_keep_unmanaged_rules():
    apply_bucket_lifecycle = _load_script("apply_bucket_lifecycle")
    desired = [
        apply_bucket_lifecycle._r2_rest_rule(rule) for rule in reclaim.build_r2_scratch_rules()
    ]
    assert {rule["conditions"]["prefix"] for rule in desired} == set(reclaim.R2_SCRATCH_PREFIXES)
    assert all(
        rule["deleteObjectsTransition"]["condition"] == {"type": "Age", "maxAge": 86400}
        for rule in desired
    )
    merged = apply_bucket_lifecycle._merge_r2_rest_rules(
        [{"id": "unmanaged", "conditions": {"prefix": "logs/"}}], desired
    )
    assert merged[0]["id"] == "unmanaged"
    assert all(rule in merged for rule in desired)


# ── gc_audio: report body reflects auto-reaped counts ────────────────────────────────


def test_issue_body_reflects_auto_reaped_count():
    # In --auto-confirm mode matured orphans WERE deleted even though args.apply is False, so the
    # persisted issue body must not read as a pure dry-run (GH#496 CodeRabbit finding #6).
    summary = {
        "by_city": {"Austin": {"files": 2, "bytes": 2048}},
        "total_files": 2,
        "total_bytes": 2048,
    }
    body = gc_audio.render_issue_body(
        summary, applied=False, auto_mode=True, auto_reaped=3, quarantine_days=21
    )
    assert "3 object(s) were auto-reaped" in body
    assert "remain" in body.lower()
    assert "dry-run" not in body.lower()  # not a plain dry-run when objects were auto-reaped


def test_issue_body_plain_dry_run_when_not_auto_mode():
    # A bare manual invocation (no --auto-confirm, no --apply) is a genuine dry-run — auto_mode
    # defaults to False, matching the real call pattern (auto_reaped is only ever >0 when auto_mode
    # is True, so this combination never occurs from main()).
    summary = {
        "by_city": {"Austin": {"files": 1, "bytes": 1024}},
        "total_files": 1,
        "total_bytes": 1024,
    }
    body = gc_audio.render_issue_body(summary, applied=False)
    assert "dry-run" in body.lower()
    assert "auto-reaped" not in body.lower()


def test_issue_body_explains_burndown_when_auto_mode_reaps_nothing_yet():
    # Scheduled run, auto-confirm enabled, but nothing matured past the quarantine window this
    # cycle (auto_reaped=0). The ticket must still explain it's under active double-confirm
    # tracking — not read as an idle plain dry-run — so a reader understands the clear timeline.
    summary = {
        "by_city": {"Austin": {"files": 5, "bytes": 5120}},
        "total_files": 5,
        "total_bytes": 5120,
    }
    body = gc_audio.render_issue_body(
        summary, applied=False, auto_mode=True, auto_reaped=0, quarantine_days=21
    )
    assert "dry-run" not in body.lower()
    assert "no objects matured for auto-reap this run" in body.lower()
    assert "≥2 scheduled runs" in body
    assert "≥21 days" in body
    assert "auto-closes on its own" in body.lower()


def test_issue_body_quarantine_days_formats_without_trailing_zero():
    summary = {"by_city": {}, "total_files": 0, "total_bytes": 0}
    body = gc_audio.render_issue_body(
        summary, applied=False, auto_mode=True, auto_reaped=1, quarantine_days=21.0
    )
    assert "≥21 days" in body  # not "21.0 days"


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


def test_reclaim_log_written_before_each_delete_survives_a_failed_delete(monkeypatch, tmp_path):
    # Three matured orphans; the storage raises while deleting the second. Because the reclaim-log
    # entry is committed BEFORE each delete, the log must already contain the key whose delete
    # then fails — a harmless phantom entry, not a silent miss (GH#496 CodeRabbit finding #5).
    # A single delete failure must not abort the sweep either (CR2-SC-19/MR-SC-02): the third key
    # is still reached and the run still completes and writes its report.
    old = (datetime.now(UTC) - timedelta(days=400)).replace(microsecond=0)
    objects = [
        ("granicus/src1/a-spec.m4a", old, 10),
        ("granicus/src1/b-spec.m4a", old, 20),
        ("granicus/src1/c-spec.m4a", old, 30),
    ]
    storage = _FakeGcStorage(objects, fail_on="granicus/src1/b-spec.m4a")

    rc = _run_gc_main(monkeypatch, tmp_path, storage, ["--apply", "--min-age-days", "0"])
    assert rc == 0

    # a and c were actually deleted; b's delete failed but did not stop the sweep.
    assert set(storage.deleted) == {"granicus/src1/a-spec.m4a", "granicus/src1/c-spec.m4a"}
    logged = {e.key for e in reclaim_log.load(tmp_path)}
    # All three are logged (log-before-delete, unconditionally) — b's entry is a harmless phantom
    # (logged but not actually removed) that would at worst self-clear as a resurrection alert.
    assert logged == {
        "granicus/src1/a-spec.m4a",
        "granicus/src1/b-spec.m4a",
        "granicus/src1/c-spec.m4a",
    }


def test_auto_confirm_failed_delete_stays_reported_not_counted_reaped(monkeypatch, tmp_path):
    # CodeRabbit (PR #877): a failed storage.delete() in --auto-confirm mode used to drop the
    # still-live object from the orphan report (since `key in auto_delete_keys` alone can't tell
    # "selected for auto-delete" from "actually deleted") and still count it in auto_reaped.
    old = (datetime.now(UTC) - timedelta(days=400)).replace(microsecond=0)
    matured_first_seen = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    objects = [
        ("granicus/src1/fails-spec.m4a", old, 10),
        ("granicus/src1/ok-spec.m4a", old, 20),
    ]
    storage = _FakeGcStorage(objects, fail_on="granicus/src1/fails-spec.m4a")
    # Pre-seed the double-confirm ledger so both keys mature on this single run
    # (run_count bumps 1 -> 2, first_seen already past the 21-day default quarantine).
    ledger = {
        "granicus/src1/fails-spec.m4a": {
            "first_seen": matured_first_seen,
            "run_count": 1,
            "last_seen": matured_first_seen,
        },
        "granicus/src1/ok-spec.m4a": {
            "first_seen": matured_first_seen,
            "run_count": 1,
            "last_seen": matured_first_seen,
        },
    }
    (tmp_path / gc_audio.ORPHAN_LEDGER_NAME).write_text(json.dumps(ledger), encoding="utf-8")

    out_dir = tmp_path / "out"
    rc = _run_gc_main(
        monkeypatch,
        tmp_path,
        storage,
        ["--auto-confirm", "--min-age-days", "0", "--out", str(out_dir)],
    )
    assert rc == 0

    assert storage.deleted == ["granicus/src1/ok-spec.m4a"]
    # The failed delete's key must still show up as a live orphan in the report...
    orphans_tsv = (out_dir / "orphans.tsv").read_text()
    assert "granicus/src1/fails-spec.m4a" in orphans_tsv
    assert "granicus/src1/ok-spec.m4a" not in orphans_tsv  # this one really is gone
    # ...and the auto-reaped count must reflect only the real deletion, not the failed one.
    body = (out_dir / "issue-body.md").read_text()
    assert "1 object(s) were auto-reaped" in body


def test_missing_last_modified_is_kept_not_treated_as_old(monkeypatch, tmp_path):
    # CR2-SC-18: a missing/non-comparable last_modified must be treated conservatively (kept),
    # not fall through the `last_modified is not None and last_modified > cutoff` guard as if it
    # were old enough to delete.
    old = (datetime.now(UTC) - timedelta(days=400)).replace(microsecond=0)
    objects = [
        ("granicus/src1/unknown-mtime-spec.m4a", None, 10),
        ("granicus/src1/old-spec.m4a", old, 20),
    ]
    storage = _FakeGcStorage(objects)
    rc = _run_gc_main(monkeypatch, tmp_path, storage, ["--apply", "--min-age-days", "0"])
    assert rc == 0
    assert storage.deleted == ["granicus/src1/old-spec.m4a"]


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


# ── gc_audio: reconcile_gc_issue — Python-owned, mirroring audit_feeds.py's reconcile() ──────
#
# GH#496 follow-up: the rolling GC issue's open/update/close logic originally lived as three
# separate workflow-YAML `if:` conditions gated on read-back JSON files — the same shape
# audit_feeds.py already solved (for a harder N-issue problem) by keeping the decision in Python
# and calling a mocked `_gh()`. These tests exercise that function directly instead of needing a
# hand-rolled GitHub Actions expression evaluator to check YAML conditions.


def _mock_gh(monkeypatch, existing_number: str | None):
    """Patch gc_audio._gh and _find_open_gc_issue; return the list of captured (args) tuples."""
    calls: list[tuple] = []
    monkeypatch.setattr(gc_audio, "_find_open_gc_issue", lambda: existing_number)

    def fake_gh(*args, check=True):
        calls.append(args)
        if args[:2] == ("issue", "create"):
            return "https://github.com/org/repo/issues/999\n"
        return ""

    monkeypatch.setattr(gc_audio, "_gh", fake_gh)
    return calls


def test_reconcile_creates_issue_when_orphans_remain_and_none_exists(monkeypatch):
    calls = _mock_gh(monkeypatch, existing_number=None)
    action = gc_audio.reconcile_gc_issue(
        "body text", has_orphans=True, applied=False, run_url="https://run"
    )
    assert action == "created"
    assert calls[0][:2] == ("issue", "create")
    assert any(c[:2] == ("issue", "edit") and "--add-label" in c for c in calls)


def test_reconcile_updates_existing_issue_when_orphans_remain(monkeypatch):
    calls = _mock_gh(monkeypatch, existing_number="42")
    action = gc_audio.reconcile_gc_issue(
        "body text", has_orphans=True, applied=False, run_url="https://run"
    )
    assert action == "updated"
    expected_body = "body text\n\n> Full object list (`orphans.tsv`): https://run\n"
    assert calls == [("issue", "edit", "42", "--body", expected_body)]


def test_reconcile_closes_after_manual_apply_regardless_of_has_orphans(monkeypatch):
    calls = _mock_gh(monkeypatch, existing_number="42")
    action = gc_audio.reconcile_gc_issue(
        "body", has_orphans=True, applied=True, run_url="https://run"
    )
    assert action == "closed-apply"
    assert ("issue", "comment", "42", "--body", "Reaped by apply run: https://run") in calls
    assert ("issue", "close", "42") in calls


def test_reconcile_closes_when_autoconfirm_clears_backlog(monkeypatch):
    # The gap this whole refactor fixes: has_orphans=False, applied=False (a scheduled
    # auto-confirm run that reaped everything) must still close the ticket.
    calls = _mock_gh(monkeypatch, existing_number="42")
    action = gc_audio.reconcile_gc_issue(
        "body", has_orphans=False, applied=False, run_url="https://run"
    )
    assert action == "closed-auto"
    assert any(c[:2] == ("issue", "comment") and "cleared" in c[-1].lower() for c in calls)
    assert ("issue", "close", "42") in calls


def test_reconcile_noop_when_nothing_outstanding_and_no_issue_exists(monkeypatch):
    calls = _mock_gh(monkeypatch, existing_number=None)
    action = gc_audio.reconcile_gc_issue("body", has_orphans=False, applied=False)
    assert action == "noop"
    assert calls == []


def test_reconcile_dry_run_never_calls_gh(monkeypatch):
    # existing_number="42" → an issue already exists, so dry-run must report the *exact* action each
    # (has_orphans, applied) case would take live (matching the non-dry-run tests above), not merely
    # "one of the valid actions": orphans remaining refresh the existing ticket ("updated"); a
    # cleared scheduled backlog closes it ("closed-auto"); a manual apply closes ("closed-apply").
    calls = _mock_gh(monkeypatch, existing_number="42")
    for has_orphans, applied, expected in (
        (True, False, "updated"),
        (False, False, "closed-auto"),
        (True, True, "closed-apply"),
    ):
        action = gc_audio.reconcile_gc_issue(
            "body", has_orphans=has_orphans, applied=applied, dry_run=True
        )
        assert action == expected
    assert calls == []  # _find_open_gc_issue is mocked, not _gh — no gh call should fire
