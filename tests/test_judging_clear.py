"""Archive-and-clear of stored judgments (citypods/judging/clear.py)."""

from __future__ import annotations

from citypods.judging import clear, ledger
from citypods.judging.tasks import EpisodeTexts, QuestionSpec, Subject, TaskSpec, make_evidence

VALIDATE = QuestionSpec("supported", "validate", "Is it supported?")
CHOOSE = QuestionSpec("best", "choose", "Which is best?")


def _spec(digest="new"):
    return TaskSpec(
        name="t",
        questions=(VALIDATE, CHOOSE),
        subjects=lambda ep: [],
        evidence=lambda subject, tier, texts: make_evidence(tier, digest),
        first_tier="T1",
        escalation=None,
        consensus_policy="x",
        selection_policy="top_k_per_group",
    )


def _row(qid, digest, at="2026-11-15T00:00:00Z", tier="T1", role="anchor", **extra):
    return {
        "subject_id": "s1",
        "question_id": qid,
        "judge_model": "m",
        "judge_role": role,
        "context_tier": tier,
        "evidence_digest": digest,
        "prompt_version": "1",
        "judged_at": at,
        "value": 0.9,
        **extra,
    }


def _subject(rows, pending=()):
    store: dict = {"subjects": {"s1": {"task": "t", "judgments": list(rows)}}}
    if pending:
        store["subjects"]["s1"]["pending"] = list(pending)
    return Subject("t", "s1", "ep", "g", "p", {}, store)


def test_stale_evidence_removes_only_rows_asked_of_different_evidence():
    spec = _spec()
    fresh = make_evidence("T1", "new").digest
    subject = _subject(
        [
            _row("supported", fresh),  # same evidence now: still valid
            _row("supported", "old-window"),  # asked of a truncated window
            _row("supported_s", "old-window"),  # its stability re-ask goes with it
            _row("best", "options-digest"),  # choose: option quotes, never stale
            _row("supported", fresh, tier=None),  # no tier: cannot tell, kept
        ],
        pending=[
            {"question_id": "supported", "context_tier": "T1", "evidence_digest": "old-window"},
            {"question_id": "best", "kind": "choose", "evidence_digest": "x"},
        ],
    )
    part = clear.partition_subject(spec, subject, EpisodeTexts(), mode="stale-evidence")
    assert [r["question_id"] for r in part.removed_rows] == ["supported", "supported_s"]
    assert [m["question_id"] for m in part.removed_pending] == ["supported"]
    assert part.kept_rows == 3 and part.unverifiable == 1


def test_stale_evidence_keeps_everything_when_the_transcript_is_unavailable():
    subject = _subject([_row("supported", "old-window")])
    part = clear.partition_subject(_spec(), subject, None, mode="stale-evidence")
    assert part.removed_rows == [] and part.unverifiable == 1


def test_before_mode_removes_every_row_judged_earlier_and_all_pending_markers():
    subject = _subject(
        [
            _row("supported", "a", at="2026-11-10T00:00:00Z"),
            _row("best", "b", at="2026-11-17T23:59:59Z"),
            _row("supported", "c", at="2026-11-18T00:00:00Z"),
        ],
        pending=[{"question_id": "supported"}],
    )
    part = clear.partition_subject(_spec(), subject, None, mode="before", before="2026-11-18")
    assert sorted(r["evidence_digest"] for r in part.removed_rows) == ["a", "b"]
    assert len(part.removed_pending) == 1 and part.kept_rows == 1


def test_apply_archive_and_restore_round_trip(tmp_path):
    subject = _subject(
        [_row("supported", "old"), _row("supported", make_evidence("T1", "new").digest)],
        pending=[{"question_id": "supported", "context_tier": "T1", "evidence_digest": "old"}],
    )
    part = clear.partition_subject(_spec(), subject, EpisodeTexts(), mode="stale-evidence")
    entries = [
        {"uid": "u", "subject_id": "s1", "kind": "judgment", "row": r} for r in part.removed_rows
    ] + [
        {"uid": "u", "subject_id": "s1", "kind": "pending", "row": m} for m in part.removed_pending
    ]
    path = tmp_path / "archive" / "x" / "src.jsonl.gz"
    clear.write_archive(path, entries)
    clear.apply_partition(subject, part)
    assert len(ledger.judgments(subject)) == 1 and ledger.pending(subject) == []
    assert "pending" not in subject.store["subjects"]["s1"]
    restored = clear.restore_entries(clear.read_archive(path), {"s1": subject}.get)
    assert (
        restored == 2 and len(ledger.judgments(subject)) == 2 and len(ledger.pending(subject)) == 1
    )
    # Restoring again changes nothing the ledger already has.
    clear.restore_entries(clear.read_archive(path), {"s1": subject}.get)
    assert len(ledger.judgments(subject)) == 2


def test_summary_counts_by_question_role_and_model():
    subject = _subject([_row("supported", "old"), _row("supported", "old2", role="sibling")])
    part = clear.partition_subject(_spec(), subject, EpisodeTexts(), mode="stale-evidence")
    summary = clear.summarize([(subject, part)])
    assert summary["removed_rows"] == 2
    assert summary["rows_by_question_role_model"] == {
        "supported|anchor|m": 1,
        "supported|sibling|m": 1,
    }


def test_an_unknown_workflow_state_aborts_instead_of_counting_as_stopped(monkeypatch):
    import subprocess
    import types

    import pytest

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(SystemExit, match="could not list judge.yml runs"):
        clear.running_judge_runs()
