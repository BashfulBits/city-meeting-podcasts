"""Review/53 PR7: the calibration report and threshold proposal (citypods/judging/calibrate.py)."""

from __future__ import annotations

import json

import yaml

from citypods.judging import calibrate
from citypods.judging.tasks import QuestionSpec, Subject, TaskSpec

ANCHOR = calibrate.ANCHOR_MODEL
FAMILIES = {ANCHOR: "google", "google/gemma-4": "google", "qwen/qwen3.8-27b": "qwen"}
VALIDATE = QuestionSpec("supported", "validate", "Is it supported?")
GATE = QuestionSpec("publishable", "gate", "Is it publishable?")
GRADE = QuestionSpec("usefulness", "grade", "How useful?", levels=("0", "1", "2", "3"))
CHOOSE = QuestionSpec("best", "choose", "Which is best?")


def _spec(*questions, selection="all_admitted"):
    return TaskSpec(
        name="t",
        questions=questions,
        subjects=lambda ep: [],
        evidence=lambda s, tier, texts: None,
        first_tier="T1",
        escalation=("T2", 0.3, 0.7),
        consensus_policy="x",
        selection_policy=selection,
    )


def _row(qid, role, model, value, *, tier="T1", sample=None, at="2026-11-15T00:00:00Z", **extra):
    row = {
        "question_id": qid,
        "judge_role": role,
        "judge_model": model,
        "value": value,
        "context_tier": tier,
        "judged_at": at,
        **extra,
    }
    if sample:
        row["sample"] = sample
    return row


def _subject(sid, rows, group=None):
    store = {"subjects": {sid: {"task": "t", "judgments": list(rows)}}}
    return Subject("t", sid, "ep", group, "p", {}, store)


def _item(anchor, sibling, adjudicator, weight=1.0):
    return calibrate.Item("s", "q", "contested", weight, anchor, sibling, adjudicator)


def test_collect_weights_samples_and_uses_only_the_gemini_anchor_since_cutover():
    contested = _subject(
        "a",
        [
            _row("supported", "anchor", ANCHOR, 0.4),
            _row("supported", "anchor", ANCHOR, 0.8, tier="T2"),  # the deciding tier
            _row("supported", "sibling", "qwen/qwen3.8-27b", True),
            _row("supported", "adjudicator", "adj", True, sample="contested", quote_valid=True),
        ],
    )
    calibration = _subject(
        "b",
        [
            _row("supported", "anchor", ANCHOR, 0.9),
            _row("supported", "sibling", "qwen/qwen3.8-27b", True),
            _row("supported", "adjudicator", "adj", True, sample="calibration", quote_valid=True),
        ],
    )
    old_anchor = _subject(
        "c",
        [
            _row("supported", "anchor", "jev/jev-1", 0.9),
            _row("supported", "anchor", ANCHOR, 0.9, at="2026-11-01T00:00:00Z"),
            _row("supported", "sibling", "qwen/qwen3.8-27b", True),
            _row("supported", "adjudicator", "adj", True, sample="contested", quote_valid=True),
        ],
    )
    failed_quote = _subject(
        "d",
        [
            _row("supported", "anchor", ANCHOR, 0.9),
            _row("supported", "sibling", "qwen/qwen3.8-27b", True),
            _row("supported", "adjudicator", "adj", True, sample="contested", quote_valid=False),
        ],
    )
    data = calibrate.collect(
        [[contested, calibration, old_anchor, failed_quote]],
        _spec(VALIDATE),
        calibration_rate=0.04,
        families=FAMILIES,
    )
    cell = data["supported"]
    assert [(i.subject_id, i.weight, i.anchor) for i in cell.items] == [
        ("a", 1.0, 0.8),
        ("b", 25.0, 0.9),
    ]
    assert cell.excluded == {calibrate.EXCLUDED_INCOMPLETE: 1}


def test_collect_ignores_a_same_family_sibling():
    subject = _subject(
        "a",
        [
            _row("supported", "anchor", ANCHOR, 0.9),
            _row("supported", "sibling", "google/gemma-4", True),
            _row("supported", "adjudicator", "adj", True, sample="contested"),
        ],
    )
    data = calibrate.collect([[subject]], _spec(VALIDATE), calibration_rate=0.03, families=FAMILIES)
    assert data["supported"].items == []
    assert data["supported"].excluded == {calibrate.EXCLUDED_INCOMPLETE: 1}


def test_items_whose_sibling_quote_is_outside_the_adjudicator_window_are_counted_out():
    def rows(sid):
        return [
            _row("supported", "anchor", ANCHOR, 0.9),
            _row("supported", "sibling", "qwen/qwen3.8-27b", True, quote_valid=True, reason=sid),
            _row("supported", "adjudicator", "adj", False, sample="contested", tier="T2"),
        ]

    subjects = [_subject(sid, rows(sid)) for sid in ("inside", "outside", "unknown")]
    verdicts = {"inside": True, "outside": False, "unknown": None}
    seen = []

    def check(subject, sibling_row, adjudicator_row):
        seen.append(adjudicator_row["context_tier"])
        return verdicts[sibling_row["reason"]]

    data = calibrate.collect(
        [subjects], _spec(VALIDATE), calibration_rate=0.03, families=FAMILIES, window_check=check
    )
    cell = data["supported"]
    assert [i.subject_id for i in cell.items] == ["inside"]
    assert cell.excluded == {calibrate.EXCLUDED_WINDOW: 1, calibrate.EXCLUDED_UNCHECKED: 1}
    assert seen == ["T2", "T2", "T2"]


def test_validate_fit_keeps_false_accepts_under_the_upper_bound_cap():
    # 60 adjudicated positives the anchor rates 0.9, 60 negatives it rates 0.1, and two negatives
    # it rates 0.85 with sibling support: admitting at 0.85 would accept those, so the fit admits
    # above them and rejects at them.
    items = [_item(0.9, True, True) for _ in range(60)]
    items += [_item(0.1, False, False) for _ in range(60)]
    items += [_item(0.85, True, False) for _ in range(2)]
    fit = calibrate.fit_validate(items, cap=0.10)["proposal"]
    assert fit["t_admit"] > 0.85 and fit["t_reject"] == 0.85
    assert fit["false_accept_rate"] == 0
    assert fit["false_accept_upper95"] <= 0.10
    assert fit["agreement"] == 1.0


def test_validate_fit_proposes_nothing_when_the_set_is_too_small_for_the_cap():
    items = [_item(0.9, True, True), _item(0.1, False, False)]
    result = calibrate.fit_validate(items, cap=0.05)
    assert result["proposal"] is None and "0.05" in result["reason"]


def test_admit_needs_the_sibling_and_calibration_weights_count():
    assert calibrate.validate_decision(_item(0.95, False, True), 0.9, 0.2) is None
    assert calibrate.validate_decision(_item(0.95, True, True), 0.9, 0.2) is True
    assert calibrate.validate_decision(_item(0.1, True, True), 0.9, 0.2) is False
    heavy = [_item(0.95, True, False, weight=33.3), _item(0.95, True, True)]
    scored = calibrate.score_validate(heavy, 0.9, 0.2)
    assert scored["false_accept_rate"] == 1.0 and scored["agreement"] == round(1 / 34.3, 4)


def test_wilson_and_kish():
    assert calibrate.wilson_upper(0.0, 0) == 1.0
    assert 0.03 < calibrate.wilson_upper(0.0, 100) < 0.04
    assert calibrate.kish([1, 1, 1, 1]) == 4
    assert calibrate.kish([1, 33]) < 2


def test_gate_fit_holds_every_adjudicator_flag():
    items = [
        _item(0.62, True, True),
        _item(0.8, True, True),
        _item(0.65, False, False),
        _item(0.2, False, False),
        _item(0.1, True, False),
    ]
    items = [
        calibrate.Item("s", "q", "contested", 1.0, i.anchor, i.sibling, i.adjudicator, "qwen")
        for i in items
    ]
    fit = calibrate.fit_gate(items)
    assert fit["proposal"] == {
        "flag_at": 0.62,
        "false_hold_rate": round(1 / 3, 4),
        "adjudicator_flags": 2,
    }
    assert fit["siblings"] == {"qwen": {"missed": 0, "false_hold_weight": 1.0}}


def test_grade_and_choose_reports():
    assert calibrate.fit_grade([_item(2, None, 3), _item(0, None, 3)]) == {"within_one_level": 0.5}
    choose = [
        _item(("a", "a"), None, "a"),
        _item(("a", "b"), None, "a"),
        _item(("b", "b"), None, "a"),
    ]
    assert calibrate.fit_choose(choose) == {"decided_share": 0.6667, "agreement_when_decided": 0.5}


def test_choose_needs_a_stable_adjudicator_in_both_orders():
    key = _subject(
        "m0",
        [
            _row("best", "anchor", ANCHOR, "m0"),
            _row("best_r", "anchor", ANCHOR, "m0"),
            _row("best", "adjudicator", "adj", "m0", sample="contested"),
            _row("best_r", "adjudicator", "adj", "m0", sample="contested"),
        ],
        group="g",
    )
    unstable = _subject(
        "n0",
        [
            _row("best", "anchor", ANCHOR, "n0"),
            _row("best_r", "anchor", ANCHOR, "n0"),
            _row("best", "adjudicator", "adj", "n0", sample="contested"),
            _row("best_r", "adjudicator", "adj", "n1", sample="contested"),
        ],
        group="h",
    )
    other = _subject("m1", [], group="g")
    data = calibrate.collect(
        [[key, other], [unstable, _subject("n1", [], group="h")]],
        _spec(CHOOSE, selection="top_k_per_group"),
        calibration_rate=0.03,
    )
    cell = data["best"]
    assert [(i.subject_id, i.anchor) for i in cell.items] == [("g", ("m0", "m0"))]
    assert cell.excluded == {"adjudicator_order_unstable": 1}


def test_report_thresholds_and_fingerprint_are_stable():
    spec = _spec(VALIDATE, GATE, GRADE)
    data = {q.id: calibrate.QuestionData(q) for q in spec.questions}
    data["supported"].items.extend(
        [_item(0.95, True, True) for _ in range(80)]
        + [_item(0.05, False, False) for _ in range(80)]
    )
    data["publishable"].items.append(_item(0.7, True, True))
    data["supported"].exclude(calibrate.EXCLUDED_WINDOW)
    result = calibrate.report(data, spec, calibration_rate=0.03)
    again = calibrate.report(data, spec, calibration_rate=0.03)
    assert result["policy_fingerprint"] == again["policy_fingerprint"]
    assert result["questions"]["supported"]["excluded"] == {calibrate.EXCLUDED_WINDOW: 1}
    block = calibrate.thresholds(result)
    assert block["anchor_model"] == ANCHOR and block["false_accept_cap"] == 0.05
    assert set(block["supported"]) == {"t_admit", "t_reject"}
    assert block["publishable"] == {"flag_at": 0.7}
    assert "usefulness" not in block
    assert "`supported`" in calibrate.markdown(result)


SITE = """\
site_name: x
judging:
  enabled: true
  tasks:
    tag: {mode: shadow}
    moment: {mode: shadow, top_k: 3}
  # a comment that must survive
  calibration_sample_rate: 0.03
other:
    tag: {mode: live}
"""


def test_write_thresholds_edits_only_the_task_line():
    block = {"policy_fingerprint": "abc", "supported": {"t_admit": 0.9, "t_reject": 0.2}}
    out = calibrate.write_thresholds(SITE, "moment", block)
    loaded = yaml.safe_load(out)
    assert loaded["judging"]["tasks"]["moment"] == {
        "mode": "shadow",
        "top_k": 3,
        "thresholds": block,
    }
    assert loaded["judging"]["tasks"]["tag"] == {"mode": "shadow"}
    assert loaded["other"]["tag"] == {"mode": "live"}
    assert "# a comment that must survive" in out
    # Writing again replaces, not nests.
    again = calibrate.write_thresholds(out, "moment", {"policy_fingerprint": "def"})
    assert yaml.safe_load(again)["judging"]["tasks"]["moment"]["thresholds"] == {
        "policy_fingerprint": "def"
    }


def test_write_thresholds_refuses_a_missing_task():
    try:
        calibrate.write_thresholds(SITE, "nope", {})
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_same_evidence_digest_needs_no_window_check_and_deferred_checks_settle():
    def rows(sid, digest):
        return [
            _row("supported", "anchor", ANCHOR, 0.9),
            _row(
                "supported",
                "sibling",
                "qwen/qwen3.8-27b",
                True,
                quote_valid=True,
                evidence_digest=digest,
            ),
            _row("supported", "adjudicator", "adj", True, sample="contested", evidence_digest="T2"),
        ]

    subjects = [_subject("same", rows("same", "T2"))]
    subjects += [_subject(sid, rows(sid, "T1")) for sid in ("in", "out", "unread")]
    asked = []

    def check(subject, sibling_row, adjudicator_row):
        asked.append(subject.subject_id)
        return calibrate.Deferred(subject.subject_id)

    data = calibrate.collect(
        [subjects], _spec(VALIDATE), calibration_rate=0.03, families=FAMILIES, window_check=check
    )
    assert asked == ["in", "out", "unread"]
    assert [i.subject_id for i in data["supported"].items] == ["same"]
    calibrate.settle_deferred(data, {"in": True, "out": False})
    cell = data["supported"]
    assert [i.subject_id for i in cell.items] == ["same", "in"]
    assert cell.excluded == {calibrate.EXCLUDED_WINDOW: 1, calibrate.EXCLUDED_UNCHECKED: 1}
    assert cell.deferred == []


def test_a_gate_level_that_holds_most_clear_items_is_a_diagnostic_not_a_proposal():
    items = [_item(0.15, True, True)] + [_item(0.2, False, False) for _ in range(9)]
    fit = calibrate.fit_gate(items)
    assert fit["proposal"] is None
    assert fit["candidate"]["flag_at"] == 0.15 and fit["candidate"]["false_hold_rate"] == 1.0


def test_gate_counts_false_holds_at_the_floored_level_that_is_exported():
    # The lowest flag is 0.629; the exported level is 0.62, which also holds the 0.625 clear item.
    items = (
        [_item(0.629, True, True)]
        + [_item(0.625, False, False)]
        + [_item(0.01, False, False) for _ in range(5)]
    )
    fit = calibrate.fit_gate(items)
    assert fit["proposal"]["flag_at"] == 0.62
    assert fit["proposal"]["false_hold_rate"] == round(1 / 6, 4)


def test_the_cap_uses_the_unrounded_bound_and_ranking_uses_unrounded_agreement():
    # 72 unit negatives and one of weight 1.2 with no false accepts: upper bound 0.0500175.
    negatives = [_item(0.1, False, False) for _ in range(72)]
    negatives.append(_item(0.1, False, False, weight=1.2))
    assert calibrate.fit_validate(negatives, cap=0.05)["proposal"] is None
    # Agreement differences below the fourth decimal still rank pairs.
    many = [_item(0.1, False, False) for _ in range(25_000)] + [_item(0.9, True, True)]
    assert calibrate.fit_validate(many, cap=0.5)["proposal"]["t_admit"] == 0.9


def test_adjudications_from_before_the_cutover_are_left_out():
    def rows(at):
        return [
            _row("supported", "anchor", ANCHOR, 0.9),
            _row("supported", "sibling", "qwen/qwen3.8-27b", True),
            _row("supported", "adjudicator", "adj", True, sample="contested", at=at),
            _row("supported", "adjudicator", "adj2", True, sample="calibration", at=at),
        ]

    old = _subject("old", rows("2026-11-10T00:00:00Z")[:3])
    old_cal = _subject("oldcal", [rows("2026-11-10T00:00:00Z")[i] for i in (0, 1, 3)])
    new = _subject("new", rows("2026-11-15T00:00:00Z")[:3])
    data = calibrate.collect(
        [[old, old_cal, new]], _spec(VALIDATE), calibration_rate=0.03, families=FAMILIES
    )
    cell = data["supported"]
    assert [i.subject_id for i in cell.items] == ["new"]
    assert cell.excluded == {calibrate.EXCLUDED_PRECUTOVER: 2}


def test_write_thresholds_handles_comments_and_stays_inside_judging_tasks():
    text = """\
judging:
  tasks:
    tag: {mode: shadow}  # keep in shadow
other:
  tasks:
    tag: {mode: live}
"""
    out = calibrate.write_thresholds(text, "tag", {"policy_fingerprint": "x"})
    assert "# keep in shadow" in out
    loaded = yaml.safe_load(out)
    assert loaded["judging"]["tasks"]["tag"]["thresholds"] == {"policy_fingerprint": "x"}
    assert loaded["other"]["tasks"]["tag"] == {"mode": "live"}
    block_style = "judging:\n  tasks:\n    tag:\n      mode: shadow\nother:\n  x: 1\n"
    try:
        calibrate.write_thresholds(block_style + "    tag: {mode: live}\n", "tag", {})
    except ValueError:
        pass
    else:
        raise AssertionError("an entry outside judging.tasks must not be edited")


def test_the_family_policy_is_part_of_the_fingerprint():
    spec = _spec(VALIDATE)
    data = {q.id: calibrate.QuestionData(q) for q in spec.questions}
    one = calibrate.report(data, spec, calibration_rate=0.03, families=FAMILIES)
    moved = {**FAMILIES, "qwen/qwen3.8-27b": "google"}
    two = calibrate.report(data, spec, calibration_rate=0.03, families=moved)
    assert one["policy_fingerprint"] != two["policy_fingerprint"]
    assert (
        one["policy_fingerprint"]
        == calibrate.report(data, spec, calibration_rate=0.03, families=dict(FAMILIES))[
            "policy_fingerprint"
        ]
    )


def test_window_cache_keys_the_quote_and_does_not_cache_read_failures(tmp_path):
    def sibling(reason):
        return {"question_id": "supported", "reason": reason, "evidence_digest": "d1"}

    adjudicator = {"evidence_digest": "d2", "context_tier": "T2"}
    subject = Subject("t", "s", "ep", None, "p", {})
    first = calibrate._check_key(subject, sibling("quote a"), adjudicator)
    assert first == calibrate._check_key(subject, sibling("quote a"), adjudicator)
    assert first != calibrate._check_key(subject, sibling("quote b"), adjudicator)

    checks = calibrate.WindowChecks(None, {}, _spec(VALIDATE))
    cache = tmp_path / "cache.json"
    checks.pending["ep"] = [(first, subject, sibling("quote a"), adjudicator)]
    checks.episodes["ep"] = type("E", (), {"transcript_key": "k"})()
    checks.storage = None  # the read fails: the verdict is not cached
    assert checks.run(cache) == {first: None}
    assert json.loads(cache.read_text())["r"] == {}
    # A cached verdict for another quote is not a hit for this one.
    cache.write_text(json.dumps({"v": 2, "r": {checks._name(("s", "supported", "other")): True}}))
    checks.pending["ep"] = [(first, subject, sibling("quote a"), adjudicator)]
    assert checks.run(cache) == {first: None}


def test_stability_probe_and_old_prompt_rows_do_not_replace_the_routine_answer():
    versioned = QuestionSpec("supported", "validate", "Is it supported?", prompt_version="2")
    subject = _subject(
        "a",
        [
            _row("supported", "anchor", ANCHOR, 0.9, prompt_version="2"),
            _row("supported", "anchor", ANCHOR, 0.1, at="2026-11-16T00:00:00Z", sample="stability"),
            _row("supported", "anchor", ANCHOR, 0.2, at="2026-11-17T00:00:00Z", prompt_version="1"),
            _row("supported", "sibling", "qwen/qwen3.8-27b", True, prompt_version="2"),
            _row("supported", "adjudicator", "adj", True, sample="contested", prompt_version="2"),
        ],
    )
    data = calibrate.collect(
        [[subject]], _spec(versioned), calibration_rate=0.03, families=FAMILIES
    )
    assert [i.anchor for i in data["supported"].items] == [0.9]


def test_choose_pairs_adjudicated_before_the_cutover_are_left_out():
    rows = [
        _row("best", "anchor", ANCHOR, "m0"),
        _row("best_r", "anchor", ANCHOR, "m0"),
        _row("best", "adjudicator", "adj", "m0", sample="contested", at="2026-11-10T00:00:00Z"),
        _row("best_r", "adjudicator", "adj", "m0", sample="contested"),
    ]
    key = _subject("m0", rows, group="g")
    data = calibrate.collect(
        [[key, _subject("m1", [], group="g")]],
        _spec(CHOOSE, selection="top_k_per_group"),
        calibration_rate=0.03,
    )
    assert data["best"].items == []
    assert data["best"].excluded == {calibrate.EXCLUDED_PRECUTOVER: 1}


def test_write_thresholds_skips_comment_lines_between_tasks():
    text = (
        "judging:\n  tasks:\n    tag: {mode: shadow}\n"
        "  # moment settings\n    moment: {mode: shadow}\n"
    )
    out = calibrate.write_thresholds(text, "moment", {"policy_fingerprint": "x"})
    assert yaml.safe_load(out)["judging"]["tasks"]["moment"]["thresholds"]


def test_a_retired_anchor_is_scored_in_its_own_era_at_the_current_thresholds():
    old = "typesafe/jev-1.13"

    def rows(anchor_p, truth, at):
        return [
            _row("supported", "anchor", old, anchor_p, at=at),
            _row("supported", "anchor", ANCHOR, 0.99, at="2026-11-15T00:00:00Z"),
            _row("supported", "sibling", "qwen/qwen3.8-27b", True),
            _row("supported", "adjudicator", "adj", truth, sample="contested", at=at),
        ]

    era = "2026-11-01T00:00:00Z"
    subjects = [_subject(f"ok{i}", rows(0.9, True, era)) for i in range(60)]
    subjects += [_subject(f"no{i}", rows(0.1, False, era)) for i in range(60)]
    subjects.append(_subject("late", rows(0.9, True, "2026-11-20T00:00:00Z")))
    data = calibrate.collect(
        [subjects],
        _spec(VALIDATE),
        calibration_rate=0.03,
        anchor_model=old,
        since=None,
        until="2026-11-12",
        families=FAMILIES,
    )
    cell = data["supported"]
    assert len(cell.items) == 120
    assert cell.excluded == {calibrate.EXCLUDED_AFTER_WINDOW: 1}
    block = {"supported": {"t_admit": 0.8, "t_reject": 0.2}}
    assert calibrate.evaluate(data, block, cap=0.1)["accepted"] is True
    # A threshold that admits the anchor's wrong answers is not accepted.
    bad = {"supported": {"t_admit": 0.05, "t_reject": 0.04}}
    wrong = calibrate.evaluate(data, bad, cap=0.1)
    assert wrong["accepted"] is False
    assert wrong["questions"]["supported"]["scored"]["false_accept_rate"] == 1.0


def test_gate_evaluation_counts_missed_flags_at_the_given_level():
    items = [_item(0.6, True, True), _item(0.2, True, True), _item(0.1, False, False)]
    out = calibrate._gate_at(items, 0.5)
    assert out["missed_flag_share"] == 0.5 and out["accepted"] is False
    assert calibrate._gate_at(items, 0.15)["accepted"] is True
