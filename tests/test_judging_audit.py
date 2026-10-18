"""review/53 PR6: the weekly blind audit, from selection to the ledger."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from citypods.judging import audit, ledger, runner
from citypods.judging import audit_cli as cli
from citypods.judging.adjudication import due_questions
from citypods.judging.tasks import make_evidence, task
from citypods.review_issues import append_envelope, decode_envelope
from tests.test_judging import (
    ADJ,
    FAMILIES,
    LANES,
    FakeDispatch,
    _ctx,
    _episode,
    _segments,
    _subjects,
    _vtt,
)

NOW = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
ANCHOR = LANES["judge:anchor"].primary_model


def _row(
    subject, role, value, *, model=None, tier="T1", quote_valid=None, sample="routine", at=NOW
):
    row = {
        "task": subject.task,
        "subject_id": subject.subject_id,
        "question_id": "supported",
        "kind": "validate",
        "judge_model": model
        or {"anchor": ANCHOR, "sibling": "google/gemma-4-31b-it"}.get(role, "j"),
        "judge_role": role,
        "prompt_version": "1",
        "context_tier": tier,
        "evidence_digest": "d",
        "packet_id": f"p-{role}-{tier}",
        "sample": sample,
        "value": value,
        "quote_valid": quote_valid,
        "judged_at": at.isoformat(),
    }
    assert ledger.append_judgment(subject, row)
    return row


def _judged(uid, *, anchor, sibling, adjudicator=None):
    ep = _episode(uid=uid, llm=[])
    [subject] = _subjects(ep)
    _row(subject, "anchor", anchor)
    _row(subject, "sibling", sibling)
    if adjudicator is not None:
        _row(subject, "adjudicator", adjudicator, model=ADJ.models[0], tier="T2")
    return ep, subject


def _gather(episodes, **extra):
    return audit.gather([("dallas-tx", ep) for ep in episodes], [task("tag")], **extra)


# ---- selection ----------------------------------------------------------------------------


def test_items_are_identified_by_subject_and_question_for_ever():
    assert audit.item_id("subj-1", "supported") == audit.item_id("subj-1", "supported")
    assert audit.item_id("subj-1", "supported") != audit.item_id("subj-2", "supported")
    assert audit.batch_id(datetime(2026, 10, 19, tzinfo=UTC)) == "2026-W43"


def test_expected_information_orders_adjudicator_disagreement_then_judge_disagreement():
    calm, _ = _judged("calm", anchor=0.95, sibling=True)
    split, _ = _judged("split", anchor=0.9, sibling=False)
    near, _ = _judged("near", anchor=0.55, sibling=True)
    overruled, _ = _judged("overruled", anchor=0.9, sibling=True, adjudicator=False)
    scored = {c.episode.uid: c for c in _gather([calm, split, near, overruled])}
    assert scored["calm"].score == 0 and scored["calm"].reasons == ()
    assert scored["split"].reasons == ("judges_disagree",)
    assert scored["near"].reasons == ("near_threshold",)
    assert "adjudicator_disagrees" in scored["overruled"].reasons
    ranked = sorted(scored, key=lambda uid: -scored[uid].score)
    assert ranked[0] == "overruled" and ranked[-1] == "calm"


def test_a_claim_needs_both_an_anchor_and_a_sibling_reading_and_is_never_asked_twice():
    ep = _episode(uid="lonely", llm=[])
    [subject] = _subjects(ep)
    _row(subject, "anchor", 0.9)
    assert _gather([ep]) == []  # nothing to compare yet
    _row(subject, "sibling", True)
    [only] = _gather([ep])
    assert _gather([ep], audited={only.item_id}) == []


def test_unsupported_votes_do_not_count_toward_information():
    ep = _episode(uid="odd", llm=[])
    [subject] = _subjects(ep)
    _row(subject, "anchor", 0.95)
    _row(subject, "sibling", False, quote_valid=False)  # its quote was not in the evidence
    assert _gather([ep]) == []


def test_a_newest_judge_is_one_first_seen_recently_after_others_were():
    old, new = NOW - timedelta(days=60), NOW - timedelta(days=3)
    ep = _episode(uid="seat", llm=[])
    [subject] = _subjects(ep)
    _row(subject, "anchor", 0.9, at=old)
    assert audit.newest_judge([ep], NOW) is None  # a single judge has no "newest"
    _row(subject, "sibling", True, model="fresh/route", at=new)
    assert audit.newest_judge([ep], NOW) == "fresh/route"
    assert audit.newest_judge([ep], NOW + timedelta(days=30)) is None  # no longer new


def test_the_noisiest_and_newest_judges_weigh_in():
    ep, subject = _judged("n", anchor=0.95, sibling=True)
    gemma = "google/gemma-4-31b-it"
    [plain] = _gather([ep])
    [boosted] = _gather([ep], noisy=gemma, newest=ANCHOR)
    assert plain.score == 0 and boosted.score == 2
    assert {"noisiest_judge", "newest_judge"} <= set(boosted.reasons)
    # Noise is the share of chat answers whose quote failed or that flipped on a re-ask.
    _row(subject, "sibling", True, model="m2", quote_valid=False)
    _row(subject, "sibling", True, model="m2", tier="T2", quote_valid=True)
    noise = audit.judge_noise([ep])
    assert noise["m2"] == 0.5 and noise[gemma] == 0.0


def test_selection_is_the_most_informative_plus_a_couple_of_random_and_capped():
    episodes = [_judged(f"e{i}", anchor=0.9, sibling=bool(i % 2))[0] for i in range(10)] + [
        _judged(f"q{i}", anchor=0.97, sibling=True)[0] for i in range(10)
    ]
    candidates = _gather(episodes)
    chosen = audit.select(candidates, limit=12, week="2026-W43")
    informative = [c for c in chosen if c.score > 0]
    # Five claims carry information; the rest of the week is two random ones, not a filled quota.
    assert len(informative) == 5 and len(chosen) == 7
    assert audit.select(candidates, limit=12, week="2026-W43") == chosen  # deterministic
    assert [c.item_id for c in audit.select(candidates, limit=12, week="2026-W44")] != [
        c.item_id for c in chosen
    ]
    assert (
        audit.select([], limit=12, week="w") == []
        and audit.select(candidates, limit=0, week="w") == []
    )
    # Only two random fillers are ever taken, so a quiet week asks fewer than the cap.
    quiet = _gather([_judged(f"z{i}", anchor=0.97, sibling=True)[0] for i in range(10)])
    assert len(audit.select(quiet, limit=12, week="w")) == 2


def test_the_weekly_count_shrinks_with_clean_probes_and_after_a_discarded_batch():
    assert audit.weekly_item_count() == 12
    assert audit.weekly_item_count(probes_clean=True) == 4
    assert audit.weekly_item_count(shrink={"tag": 0.5}) == 6
    assert audit.weekly_item_count(probes_clean=True, shrink={"tag": 0.25}) == 2


# ---- rendering ----------------------------------------------------------------------------


def test_a_claim_is_framed_to_disprove_and_hides_every_verdict():
    ep, _ = _judged("r", anchor=0.9, sibling=False, adjudicator=True)
    [candidate] = _gather([ep])
    body = audit.render_child(candidate, "Proposed tag: Zoning\nTranscript: council voted yes")
    assert "Try to disprove it" in body and "> Proposed tag: Zoning" in body
    assert "- [ ] Supported" in body and "- [ ] Not supported" in body and "- [ ] Uncertain" in body
    assert "Rubric" in body and candidate.question.instruction in body
    for leak in ("anchor", "sibling", "adjudicator", "0.9", "gemma", "jev", "contested", "judges"):
        assert leak not in body.lower().replace("the automated verdicts are hidden", "")
    assert "Listen" not in body
    with_audio = audit.render_child(candidate, "x", audio_url="https://e.test/m/#t=42")
    assert "Listen to the moment: https://e.test/m/#t=42" in with_audio
    parent = audit.render_parent("2026-W43", 3, {"tag": 2, "moment": 1})
    assert "3 claim(s) (1 moment, 2 tag)" in parent


# ---- answers, settling and the ledger -----------------------------------------------------


EVIDENCE = "Proposed tag: x\nTranscript: some words"


def _state(answers, *, task_name="tag", created=NOW - timedelta(days=7), kinds=None):
    state = audit.AuditState()
    ids = []
    for n, verdict in enumerate(answers):
        ident = f"item-{n}"
        ids.append(ident)
        state.items[ident] = {
            "task": task_name,
            "subject_id": f"subj-{n}",
            "question_id": "supported",
            "tier": "T2",
            "evidence_digest": make_evidence("T2", EVIDENCE).digest,
            "evidence": EVIDENCE,
            "item_type": (kinds or {}).get(n, "tag:llm"),
            "batch": "2026-W42",
        }
        if verdict:
            state.answers[ident] = {
                "verdict": verdict,
                "actor": "me",
                "issue": n,
                "at": NOW.isoformat(),
            }
    state.batches["2026-W42"] = {
        "created": created.isoformat(),
        "status": "open",
        "item_ids": ids,
    }
    return state


def test_an_answer_is_stored_replaced_until_settled_and_never_invented():
    state = _state([None])
    first = audit.record_answer(state, "item-0", "Supported", actor="a", issue=3, now=NOW)
    assert first == {"stored": True, "item_id": "item-0", "verdict": "supported"}
    audit.record_answer(state, "item-0", "Not supported", actor="a", issue=3, now=NOW)
    assert state.answers["item-0"]["verdict"] == "not_supported"
    assert state.answers["item-0"]["revisions"] == 1
    with pytest.raises(ValueError, match="not in the durable ledger"):
        audit.record_answer(state, "nope", "Supported", actor="a", issue=1, now=NOW)
    with pytest.raises(ValueError, match="unknown audit choice"):
        audit.record_answer(state, "item-0", "Maybe", actor="a", issue=1, now=NOW)
    state.batches["2026-W42"]["status"] = "finalized"
    assert audit.record_answer(state, "item-0", "Supported", actor="a", issue=3, now=NOW) == {
        "stored": False,
        "reason": "batch_settled",
        "item_id": "item-0",
    }


def test_a_young_batch_is_left_open_and_an_old_one_is_settled_by_task():
    young = _state(["supported"], created=NOW - timedelta(days=2))
    assert audit.settle(young, NOW) == [] and young.batches["2026-W42"]["status"] == "open"
    old = _state(["supported", "not_supported", "supported", "uncertain"])
    [result] = audit.settle(old, NOW)
    assert (result.answered, result.uncertain, result.discarded) == (4, 1, False)
    assert old.batches["2026-W42"]["status"] == "finalized" and not old.shrink
    assert old.batches["2026-W42"]["tasks"] == {"tag": "finalized"}


def test_too_many_uncertain_answers_discard_the_labels_and_shrink_the_next_batch():
    state = _state(
        ["uncertain", "uncertain", "supported", "uncertain"],
        kinds={0: "tag:rule", 1: "tag:rule", 3: "tag:llm"},
    )
    [result] = audit.settle(state, NOW)
    assert result.discarded and result.uncertain == 3
    assert result.failing_types == (("tag:rule", 2), ("tag:llm", 1))
    assert state.shrink == {"tag": 0.5} and audit.weekly_item_count(shrink=state.shrink) == 6
    assert audit.import_rows(state) == {}  # none of that batch's labels are used
    audit.settle(_state(["uncertain"]), NOW)  # a second settling is a no-op for settled batches
    again = _state(["uncertain", "uncertain"])
    audit.settle(again, NOW)
    audit.settle(again, NOW)
    assert again.shrink == {"tag": 0.5}


def test_finalized_answers_become_audit_rows_and_uncertain_ones_carry_no_value():
    state = _state(["supported", "not_supported", "uncertain", "supported"])
    audit.settle(state, NOW)
    rows = audit.import_rows(state)
    assert sorted(rows) == ["subj-0", "subj-1", "subj-2", "subj-3"]
    [yes] = rows["subj-0"]
    assert (yes["judge_role"], yes["judge_model"], yes["sample"], yes["value"]) == (
        "audit",
        "human",
        "audit",
        True,
    )
    assert rows["subj-1"][0]["value"] is False and rows["subj-2"][0]["value"] is None
    open_batch = _state(["supported"], created=NOW)
    audit.settle(open_batch, NOW)
    assert audit.import_rows(open_batch) == {}  # an open batch's answers are not used yet


def test_a_finalized_answer_is_imported_once_and_keeps_its_episode_dirty_until_then():
    ep, subject = _judged("imp", anchor=0.9, sibling=True)
    ep.transcript_key = "t.vtt"
    state = audit.AuditState()
    state.items["i"] = {
        "task": "tag",
        "subject_id": subject.subject_id,
        "question_id": "supported",
        "tier": "T2",
        "evidence_digest": "d",
        "evidence": "x",
        "item_type": "tag:rule",
        "batch": "w",
    }
    state.answers["i"] = {
        "verdict": "not_supported",
        "actor": "me",
        "issue": 1,
        "at": NOW.isoformat(),
    }
    state.batches["w"] = {
        "created": (NOW - timedelta(days=9)).isoformat(),
        "status": "open",
        "item_ids": ["i"],
    }
    audit.settle(state, NOW)
    rows = audit.import_rows(state)
    kwargs = {
        "anchor": LANES["judge:anchor"],
        "sibling": LANES["judge:sibling"],
        "families": FAMILIES,
        "all_tiers_sample_rate": 0.0,
        "audit_rows": rows,
    }
    assert runner.episode_needs_judging(ep, [task("tag")], **kwargs)
    ctx = _ctx(FakeDispatch(), audit_rows=rows)
    stats = runner.JudgingStats()
    runner._import_audit([subject], ctx, stats)
    assert stats.counts["audit_imported"] == 1
    assert not runner.episode_needs_judging(ep, [task("tag")], **kwargs)
    again = runner.JudgingStats()
    runner._import_audit([subject], ctx, again)
    assert again.counts["audit_imported"] == 0
    assert [r["value"] for r in ledger.judgments(subject) if r["judge_role"] == "audit"] == [False]


# ---- one vote, never an override ----------------------------------------------------------


def test_a_person_who_disagrees_queues_adjudication_and_flips_nothing():
    ep, subject = _judged("dis", anchor=0.95, sibling=True)  # the judges agree and are confident
    kwargs = {"anchor_model": ANCHOR, "calibration_rate": 0.0}
    assert due_questions(subject, task("tag"), **kwargs) == []
    _row(subject, "audit", False, model="human", tier="T2", sample="audit")
    [due] = due_questions(subject, task("tag"), **kwargs)
    assert due.sample == "contested"
    # Nothing was overridden: the judges' rows are untouched.
    assert {r["value"] for r in ledger.judgments(subject) if r["judge_role"] == "anchor"} == {0.95}
    stats = runner.run([ep], _ctx(FakeDispatch(), adjudicator=ADJ))
    assert stats.counts["adjudication_contested"] == 1


def test_a_person_who_agrees_or_is_unsure_queues_nothing():
    ep, subject = _judged("agree", anchor=0.95, sibling=True)
    _row(subject, "audit", True, model="human", tier="T2", sample="audit")
    assert due_questions(subject, task("tag"), anchor_model=ANCHOR, calibration_rate=0.0) == []
    ep2, subject2 = _judged("unsure", anchor=0.95, sibling=True)
    _row(subject2, "audit", None, model="human", tier="T2", sample="audit")
    assert due_questions(subject2, task("tag"), anchor_model=ANCHOR, calibration_rate=0.0) == []


def test_repeated_disagreement_with_the_adjudicator_raises_a_ticket_not_a_policy_change():
    episodes = []
    for n in range(3):
        ep, subject = _judged(f"adj{n}", anchor=0.9, sibling=True, adjudicator=True)
        _row(subject, "audit", False, model="human", tier="T2", sample="audit")
        episodes.append(ep)
    assert audit.disagreements(episodes, [task("tag")], NOW) == {"tag": 3}
    [ticket] = cli._tickets([], {"tag": 3})
    assert "audit and adjudicator disagree on tag" in ticket["title"]
    assert cli._tickets([], {"tag": 2}) == []  # two is not "repeated"
    old = datetime(2026, 8, 1, tzinfo=UTC)
    stale, subject = _judged("stale", anchor=0.9, sibling=True, adjudicator=True)
    _row(subject, "audit", False, model="human", tier="T2", sample="audit", at=old)
    assert audit.disagreements([stale], [task("tag")], NOW) == {}


def test_a_discarded_batch_files_a_ticket_naming_the_failing_item_types():
    state = _state(["uncertain", "uncertain", "supported"], kinds={0: "tag:rule", 1: "tag:rule"})
    tickets = cli._tickets(audit.settle(state, NOW), {})
    [ticket] = tickets
    assert "tag audit format failed" in ticket["title"]
    assert "tag:rule (2)" in ticket["body"] and "discarded" in ticket["body"]


# ---- the command line ---------------------------------------------------------------------


class _Layout:
    """Stands in for storage and state sync: the state directory is the whole store."""

    def __init__(self, tmp_path):
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()
        self.pushed: list[list] = []


@pytest.fixture
def layout(tmp_path, monkeypatch):
    fake = _Layout(tmp_path)
    monkeypatch.setattr(
        cli,
        "_settings",
        lambda args: ({"judging": {"tasks": {"tag": {}}}}, fake.state_dir, object()),
    )
    monkeypatch.setattr("citypods.statesync.pull_state", lambda *a, **k: 0)
    monkeypatch.setattr(
        "citypods.statesync.push_state",
        lambda storage, state_dir, **k: fake.pushed.append(k["only_paths"]),
    )
    return fake


def _issue_body(ident, *, checked):
    candidate = SimpleNamespace(
        task="tag",
        question=task("tag").question("supported"),
    )
    body = audit.render_child(candidate, "Proposed tag: x\nTranscript: some words")
    if checked:
        body = body.replace(f"- [ ] {checked}", f"- [x] {checked}")
    return append_envelope(body, family=audit.FAMILY, candidate_id=ident)


def test_ingest_records_the_checked_box_through_the_shared_envelope(layout, tmp_path):
    state = _state([None])
    state.save(layout.state_dir)
    path = tmp_path / "issue.md"
    path.write_text(_issue_body("item-0", checked="Not supported"))
    args = SimpleNamespace(issue_body_file=str(path), issue_number=7, actor="maintainer")
    assert cli.ingest(args) == 0
    stored = audit.AuditState.load(layout.state_dir)
    assert stored.answers["item-0"]["verdict"] == "not_supported"
    assert stored.answers["item-0"]["issue"] == 7 and layout.pushed  # answers pushed

    # No box, two boxes and an unknown item are all refused in words the resolver understands.
    path.write_text(_issue_body("item-0", checked=None))
    with pytest.raises(ValueError, match="exactly one"):
        cli.ingest(args)
    path.write_text(_issue_body("missing", checked="Supported"))
    with pytest.raises(ValueError, match="no longer present in durable records"):
        cli.ingest(args)
    assert decode_envelope(path.read_text())["family"] == "judge-audit"


def test_the_resolver_routes_the_family_to_this_command():
    from scripts import resolve_review_issue

    assert resolve_review_issue.COMMANDS["judge-audit"] == ("judge-audit", "ingest")


def test_a_clear_cut_finalized_answer_is_promoted_to_gold_once(layout, tmp_path):
    state = _state(["supported", "supported", "supported", "uncertain"])
    audit.settle(state, NOW)
    state.save(layout.state_dir)
    root = tmp_path / "repo"
    (root / "evals" / "judge" / "tag").mkdir(parents=True)
    (root / "evals" / "judge" / "tag" / "gold.json").write_text("[]\n")
    args = SimpleNamespace(item_id="item-0", repo_root=str(root))
    assert cli.promote(args) == 0
    gold = json.loads((root / "evals" / "judge" / "tag" / "gold.json").read_text())
    assert [(g["id"], g["verdict"], g["source"]) for g in gold] == [
        ("tag-item-0", True, "audit:item-0")
    ]
    assert cli.promote(args) == 0  # again: already promoted, nothing duplicated
    assert len(json.loads((root / "evals" / "judge" / "tag" / "gold.json").read_text())) == 1
    for refused in ("item-3", "nope"):  # Uncertain is never a label; unknown has no answer
        with pytest.raises(ValueError):
            cli.promote(SimpleNamespace(item_id=refused, repo_root=str(root)))
    open_state = _state(["supported"], created=NOW)
    with pytest.raises(ValueError, match="not finalized"):
        cli.promote_entry(open_state, "item-0", NOW)


def test_promotion_requires_evidence_that_matches_the_asked_digest():
    state = _state(["supported"])
    audit.settle(state, NOW)
    assert cli.promote_entry(state, "item-0", NOW)[0] == "tag"
    state.items["item-0"]["evidence"] += " (edited after it was asked)"
    with pytest.raises(ValueError, match="differs from the digest"):
        cli.promote_entry(state, "item-0", NOW)


def test_package_picks_the_week_writes_the_manifest_and_is_idempotent(
    layout, tmp_path, monkeypatch
):
    ep, subject = _judged("pk", anchor=0.9, sibling=False)
    ep.transcript_key, ep.transcript_format = "t.vtt", "vtt"
    ep.title = "Council"
    monkeypatch.setattr(
        cli,
        "_episodes",
        lambda site, state_dir, config_dir: iter([(SimpleNamespace(slug="dallas-tx"), ep)]),
    )
    monkeypatch.setattr(
        "citypods.stages._read_storage_bytes", lambda storage, key: _vtt(_segments())
    )
    monkeypatch.setattr(
        "citypods.judging.texts.tag_definitions", lambda: {"zoning-reform": "Zoning reform: x"}
    )
    out = tmp_path / "out"
    args = SimpleNamespace(out_dir=str(out), config_dir="config")
    assert cli.package(args) == 0
    manifest = json.loads((out / "review-batch.json").read_text())
    [child] = manifest["children"]
    assert child["candidate_id"] == audit.item_id(subject.subject_id, "supported")
    body = (out / child["body_file"]).read_text()
    assert "Try to disprove it" in body and "Proposed tag" in body and "Transcript" in body
    assert (out / "parent.md").exists()
    stored = audit.AuditState.load(layout.state_dir)
    item = stored.items[child["candidate_id"]]
    assert (
        item["tier"] == "T2"
        and item["reasons"] == ["judges_disagree"]
        and item["batch"] in stored.batches
    )
    assert layout.pushed

    # The same week again packages nothing new: a person is never asked twice.
    second = tmp_path / "again"
    assert cli.package(SimpleNamespace(out_dir=str(second), config_dir="config")) == 0
    assert json.loads((second / "review-batch.json").read_text())["children"] == []


def test_the_probe_set_is_clean_only_when_gating_and_accurate(monkeypatch):
    from citypods.judging import probes as probe_module

    spec = task("tag")
    assert not cli.probes_clean([spec], {}) and not cli.probes_clean([], {"m": 1.0})
    monkeypatch.setattr(cli, "load_probes", lambda s: [object()] * 20)
    monkeypatch.setattr(cli, "gating", probe_module.gating)
    assert cli.probes_clean([spec], {"a": 0.97, "b": 0.99})
    assert not cli.probes_clean([spec], {"a": 0.97, "b": 0.9})
    monkeypatch.setattr(cli, "load_probes", lambda s: [object()] * 19)
    assert not cli.probes_clean([spec], {"a": 1.0})


def test_tickets_are_filed_once_and_an_open_duplicate_is_left_alone(tmp_path, monkeypatch):
    from scripts import file_judge_audit_tickets as filer

    manifest = tmp_path / "review-batch.json"
    manifest.write_text(
        json.dumps(
            {
                "tickets": [
                    {"title": "Judge audit: audit and adjudicator disagree on tag", "body": "b1"},
                    {"title": "Judge audit 2026-W43: the moment audit format failed", "body": "b2"},
                ]
            }
        )
    )
    calls: list[tuple[str, ...]] = []

    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("issue", "list"):
            return json.dumps([{"title": "Judge audit: audit and adjudicator disagree on tag"}])
        return ""

    monkeypatch.setattr(filer, "_gh", fake_gh)
    assert filer.main(["--manifest", str(manifest)]) == 0
    created = [c for c in calls if c[:2] == ("issue", "create")]
    assert len(created) == 1 and "moment audit format failed" in created[0][3]
    quiet = tmp_path / "quiet.json"
    quiet.write_text(json.dumps({"children": []}))
    calls.clear()
    assert filer.main(["--manifest", str(quiet)]) == 0 and calls == []


def test_the_audit_workflows_are_main_only_and_validate_the_promote_input():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    weekly = yaml.safe_load((root / "judge-audit.yml").read_text())
    promote = yaml.safe_load((root / "judge-audit-promote.yml").read_text())
    for flow in (weekly, promote):
        job = next(iter(flow["jobs"].values()))
        assert job["if"] == "github.ref == 'refs/heads/main'"
    assert weekly["permissions"] == {"contents": "read", "issues": "write"}
    text = (root / "judge-audit-promote.yml").read_text()
    assert "${{ inputs.item_id }}" in text and "^[0-9a-f]{24}$" in text
    # The input reaches the shell only through an environment variable, never inline.
    run_blocks = [step["run"] for step in promote["jobs"]["promote"]["steps"] if "run" in step]
    assert not any("inputs.item_id" in block for block in run_blocks)
