"""review/53 PR3: the judge stack's registry, evidence, packing, backends, ledger and runner."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_lanes import load_families, load_lanes
from citypods.judging import ledger, runner
from citypods.judging.backends import (
    ChatJudgeBackend,
    Item,
    JevBackend,
    Packet,
    choose_options,
    ensure_contract,
)
from citypods.judging.families import RULE_FAMILY, adjudicator_for, family_of, sibling_for
from citypods.judging.packing import pack
from citypods.judging.subjects import tag_subject_id
from citypods.judging.tasks import (
    EpisodeTexts,
    QuestionSpec,
    Subject,
    TaskSpec,
    make_evidence,
    register,
    registered,
    task,
)

FIXTURE = Path(__file__).parent / "fixtures" / "jev" / "answers_2026_09_30.json"
LANES = load_lanes()
FAMILIES = load_families()
GEMINI = "gemini/gemini-3.1-flash-lite"


def _segments(n=200, seconds=10.0, word="budget"):
    return [
        {"start": i * seconds, "end": (i + 1) * seconds, "text": f"{word} line {i}"}
        for i in range(n)
    ]


def _rule_tag(**extra):
    return {
        "id": "zoning-reform",
        "source_kind": "rule",
        "rule_version": "7",
        "provider_model": "rule:7",
        "chapter_id": "ch-1",
        "evidence": [{"where": "transcript", "span": "rezoning the corridor", "t": 300}],
        "display": True,
        "admission": "admitted",
        **extra,
    }


def _llm_tag(**extra):
    return {
        "id": "housing-affordability",
        "source_kind": "llm",
        "provider_model": GEMINI,
        "chapter_id": "ch-1",
        "evidence": [{"where": "transcript", "span": "rents keep rising", "t": 320}],
        "display": False,
        "admission": "shadow",
        **extra,
    }


def _moment(quote, start, **extra):
    return {
        "quote": quote,
        "why": "clear statement",
        "quote_start": start,
        "quote_end": start + 8,
        "start": start,
        "end": start + 20,
        "provider_model": "gemini/gemini-3.8-flash",
        "prompt_version": "3",
        "chapter_id": "ch-1",
        "admission": "shadow",
        "display": False,
        **extra,
    }


def _episode(uid="ep-1", published="2026-10-01", tags=None, llm=None, moments=None):
    return SimpleNamespace(
        uid=uid,
        guid=uid,
        published=published,
        tags=tags if tags is not None else [_rule_tag()],
        llm_tag_candidates=llm if llm is not None else [_llm_tag()],
        moment_pullquote_candidates=moments or [],
        chapters=[{"start": 0, "title": "Rezoning"}, {"start": 1200, "title": "Budget"}],
    )


TEXTS = EpisodeTexts(
    segments=_segments(),
    chapters=(("ch-1", "Rezoning", 0.0, 1200.0), ("ch-2", "Budget", 1200.0, None)),
    definitions={"zoning-reform": "Zoning reform: a change to the zoning code"},
)


# ---- registry ------------------------------------------------------------------------------


def test_both_tasks_register():
    assert registered() == ("moment", "tag")
    assert [q.kind for q in task("moment").questions] == ["validate", "gate", "grade", "choose"]
    assert task("tag").escalation == ("T2", 0.3, 0.7)


@pytest.mark.parametrize(
    ("questions", "policy", "match"),
    [
        (
            (QuestionSpec("a", "validate", "x"), QuestionSpec("a", "gate", "y")),
            "all_admitted",
            "repeats",
        ),
        ((QuestionSpec("g", "grade", "x"),), "all_admitted", "needs levels"),
        ((QuestionSpec("c", "choose", "x"),), "all_admitted", "top_k_per_group"),
    ],
)
def test_invalid_task_specs_are_rejected(questions, policy, match):
    spec = TaskSpec(
        name="bad",
        questions=questions,
        subjects=lambda ep: [],
        evidence=lambda s, t, x: None,
        first_tier="T1",
        escalation=None,
        consensus_policy="x",
        selection_policy=policy,
    )
    with pytest.raises(ValueError, match=match):
        register(spec)


# ---- subject identity and evidence ----------------------------------------------------------


def test_subject_id_ignores_prelabeler_and_display_but_tracks_evidence():
    base = _rule_tag()
    same = _rule_tag(prelabeler_decision="likely_incorrect", prelabeler_reason="x", display=False)
    assert tag_subject_id("ep-1", base) == tag_subject_id("ep-1", same)
    moved = _rule_tag(evidence=[{"where": "transcript", "span": "other", "t": 300}])
    assert tag_subject_id("ep-1", base) != tag_subject_id("ep-1", moved)
    assert tag_subject_id("ep-1", base) != tag_subject_id("ep-1", _rule_tag(id="other"))


def test_tag_evidence_tiers_and_caps():
    spec = task("tag")
    [subject] = [s for s in spec.subjects(_episode(llm=[])) if s.task == "tag"]
    t0 = spec.evidence(subject, "T0", TEXTS)
    t1 = spec.evidence(subject, "T1", TEXTS)
    t2 = spec.evidence(subject, "T2", TEXTS)
    assert "rezoning the corridor" in t0.text
    # T1 is plus or minus 45 s around t=300: segments 25..34 (10 s each).
    body = t1.text.split("Transcript:\n", 1)[1]
    assert body.startswith("budget line 25") and body.endswith("budget line 34")
    # T2 is the chapter (0-1200 s), capped at 1,800 words.
    assert len(t2.text.split("Transcript:\n", 1)[1].split()) <= 1800
    assert "Zoning reform: a change" in t1.text and "Chapter: Rezoning" in t1.text
    assert len({t0.digest, t1.digest, t2.digest}) == 3
    assert spec.evidence(subject, "T1", EpisodeTexts()) is None


def test_moment_evidence_is_the_quote_with_context():
    spec = task("moment")
    ep = _episode(moments=[_moment("We will fix the bridge", 600)])
    [subject] = spec.subjects(ep)
    t1 = spec.evidence(subject, "T1", TEXTS)
    assert "We will fix the bridge" in t1.text and "budget line 55" in t1.text
    assert "Agenda item: Rezoning" in spec.evidence(subject, "T2", TEXTS).text
    assert spec.evidence(subject, "T0", TEXTS) is None


# ---- families ----------------------------------------------------------------------------------


def test_sibling_and_adjudicator_are_chosen_per_entry():
    sibling, adjudicator = LANES["judge:sibling"], LANES["judge:adjudicator"]
    assert family_of(None, FAMILIES) == RULE_FAMILY
    assert family_of("rule:7", FAMILIES) == RULE_FAMILY
    assert FAMILIES[sibling_for(None, sibling, FAMILIES)] == "google"
    gemini_sibling = sibling_for(GEMINI, sibling, FAMILIES)
    assert gemini_sibling == "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    # Qwen is eligible, never active, so it is never chosen.
    assert "qwen/qwen3.8-27b" not in {sibling_for(m, sibling, FAMILIES) for m in FAMILIES}
    chosen = adjudicator_for("zai/glm-5.3-flash", gemini_sibling, adjudicator, FAMILIES)
    assert chosen == "deepseek/deepseek-v4.1-flash"
    assert adjudicator_for("deepseek/deepseek-v4-flash", None, adjudicator, FAMILIES) == (
        "zai/glm-5.3-flash"
    )
    with pytest.raises(ValueError, match="no llm_families entry"):
        family_of("unknown/model", FAMILIES)


# ---- packing -----------------------------------------------------------------------------------


def _item(subject_id, tokens, kind="validate"):
    subject = Subject("tag", subject_id, "ep", None, None, {})
    return Item(
        QuestionSpec("q", kind, "x"), "T1", make_evidence("T1", "w" * 4 * tokens), (subject,)
    )


def test_packing_respects_ceilings_and_never_splits_a_unit():
    backend = ChatJudgeBackend(max_total_tokens=1000, max_items=3)
    units = [[_item("a", 400)], [_item("b", 400)], [_item("c", 400)], [_item("d", 5000)]]
    units.append([_item("e", 100), _item("f", 100), _item("g", 100)])
    result = pack(units, backend, purpose="judge:sibling", role="sibling", judge_model="m")
    sizes = [[i.subjects[0].subject_id for i in p.items] for p in result.packets]
    assert sizes == [["a", "b"], ["c"], ["e", "f", "g"]]
    assert result.payload_too_large == 1
    again = pack(units, backend, purpose="judge:sibling", role="sibling", judge_model="m")
    assert [p.recipe_hash for p in again.packets] == [p.recipe_hash for p in result.packets]


# ---- backends ----------------------------------------------------------------------------------


def _fixture_answer(kind):
    answers = json.loads(FIXTURE.read_text())["answers"]
    return next(v for v in answers.values() if v["type"] == kind)


def test_jev_compiles_and_parses_every_question_type():
    subjects = tuple(Subject("moment", f"s{i}", "ep", "ep", None, {}) for i in range(4))
    levels = ("1", "2", "3", "4", "5")
    items = (
        Item(
            QuestionSpec("supported", "validate", "x"), "T1", make_evidence("T1", "e"), subjects[:1]
        ),
        Item(
            QuestionSpec("usefulness", "grade", "x", levels),
            "T1",
            make_evidence("T1", "e"),
            subjects[:1],
        ),
        Item(
            QuestionSpec("best", "choose", "x"),
            "T1",
            make_evidence("T1", "A: q"),
            subjects,
            options=choose_options(subjects, reverse=False),
        ),
    )
    packet = Packet("judge:anchor", "anchor", "typesafe/jev-1.13", items)
    job = JevBackend().build(packet)
    questions = job.inputs["systemone"]["questions"]
    assert [questions[i.qid]["type"] for i in items] == ["noul", "score", "choice"]
    assert questions[items[1].qid]["criteria"] == list(levels)
    assert "messages" not in job.inputs and job.inputs["llm_policy"].purpose == "judge:anchor"
    output = {
        "answers": {
            items[0].qid: _fixture_answer("noul"),
            items[1].qid: _fixture_answer("score"),
            items[2].qid: _fixture_answer("choice"),
        }
    }
    rows = JevBackend().parse(packet, output)
    by_q = {}
    for subject, row in rows:
        by_q.setdefault(row["question_id"], []).append((subject.subject_id, row))
    assert by_q["supported"][0][1]["value"] == pytest.approx(_fixture_answer("noul")["noul"])
    assert by_q["usefulness"][0][1]["value"] == pytest.approx(_fixture_answer("score")["score"])
    winner_letter = _fixture_answer("choice")["choice"]
    assert {row["value"] for _, row in by_q["best"]} == {dict(items[2].options)[winner_letter]}
    assert len(by_q["best"]) == 4  # recorded on every subject in the meeting
    # A missing answer yields no row, never a guess.
    assert JevBackend().parse(packet, {"answers": {}}) == []


def test_chat_judge_parses_valid_partial_and_malformed_replies():
    ensure_contract()
    subject = Subject("tag", "s1", "ep", None, None, {})
    item = Item(
        QuestionSpec("supported", "validate", "x"), "T1", make_evidence("T1", "e"), (subject,)
    )
    packet = Packet("judge:sibling", "sibling", "google/gemma-4-31b-it", (item,))
    job = ChatJudgeBackend(max_total_tokens=10_000, max_items=25).build(packet)
    assert job.inputs["structured_output"] == "judge-answers-v1"

    def reply(content):
        return {"choices": [{"message": {"content": content}}]}

    good = json.dumps({"answers": [{"id": item.qid, "verdict": True, "reason": "e"}]})
    [(_, row)] = ChatJudgeBackend(max_total_tokens=1, max_items=1).parse(packet, reply(good))
    assert row["value"] is True and row["reason"] == "e"
    partial = json.dumps({"answers": [{"id": "other", "verdict": True}]})
    assert ChatJudgeBackend(max_total_tokens=1, max_items=1).parse(packet, reply(partial)) == []
    assert ChatJudgeBackend(max_total_tokens=1, max_items=1).parse(packet, reply("not json")) == []


# ---- ledger ------------------------------------------------------------------------------------


def test_ledger_is_append_only_and_deduplicated():
    candidate = {}
    row = {k: "x" for k in ledger.KEY_FIELDS}
    assert ledger.append_judgment(candidate, row)
    assert not ledger.append_judgment(candidate, dict(row))
    assert len(ledger.judgments(candidate)) == 1
    ledger.add_pending(candidate, {**row, "evidence_digest": "y"})
    ledger.add_pending(candidate, {**row, "evidence_digest": "y"})
    assert len(ledger.pending(candidate)) == 1
    ledger.drop_pending(candidate, {**row, "evidence_digest": "y"})
    assert "judge_pending" not in candidate


# ---- runner ------------------------------------------------------------------------------------


class FakeDispatch:
    """Submissions return pending handles; a later pass finds answers by recipe hash."""

    def __init__(self):
        self.submitted: dict[str, tuple[Packet, object]] = {}
        self.records: dict[str, object] = {}
        self.fail = False

    def submit(self, packet, job):
        if self.fail:
            raise RuntimeError("ingress down")
        self.submitted[packet.recipe_hash] = (packet, job)
        handle = JobHandle(
            task="judge", recipe_hash=packet.recipe_hash, backend="v2", ref=packet.recipe_hash
        )
        self.records[packet.recipe_hash] = handle
        return handle

    def answer(self, anchor_p=0.9, sibling=True):
        for recipe, (_packet, job) in self.submitted.items():
            output = answer_job(job, anchor_p=anchor_p, sibling=sibling)
            self.records[recipe] = JobResult(task="judge", recipe_hash=recipe, output=output)
        self.submitted.clear()


def answer_job(job, *, anchor_p=0.9, sibling=True):
    """A plausible reply to one judge job: JEV answers keyed by qid, or a sibling chat reply."""
    if "systemone" in job.inputs:
        answers = {}
        for qid, question in job.inputs["systemone"]["questions"].items():
            if question["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": anchor_p}
            elif question["type"] == "score":
                answers[qid] = {"type": "score", "score": 2.0}
            else:
                answers[qid] = {"type": "choice", "choice": "A"}
        return {"answers": answers}
    items = json.loads(job.inputs["messages"][1]["content"])["items"]
    answers = []
    for entry in items:
        kind = entry["kind"]
        answer = {"id": entry["id"], "reason": "quoted"}
        if kind in ("validate", "gate"):
            answer["verdict"] = sibling if kind == "validate" else False
        elif kind == "grade":
            answer["level"] = 1
        else:
            answer["choice"] = "A"
        answers.append(answer)
    return {"choices": [{"message": {"content": json.dumps({"answers": answers})}}]}


def _ctx(dispatch, tasks=("tag",), **extra):
    return runner.JudgingContext(
        tasks=[task(name) for name in tasks],
        anchor=LANES["judge:anchor"],
        sibling=LANES["judge:sibling"],
        families=FAMILIES,
        texts_for=lambda ep: TEXTS,
        look_up=lambda recipe: dispatch.records.get(recipe),
        submit=dispatch.submit,
        all_tiers_sample_rate=0.0,
        **extra,
    )


def _tag_judgments(ep):
    return [row for tag in [*ep.tags, *ep.llm_tag_candidates] for row in ledger.judgments(tag)]


def test_a_judge_pass_records_anchor_and_independent_sibling_judgments():
    dispatch = FakeDispatch()
    ep = _episode()
    before = copy.deepcopy(ep)
    first = runner.run([ep], _ctx(dispatch))
    assert first.counts["packets:anchor"] == 1 and first.counts["packets:sibling"] == 2
    assert all(ledger.pending(tag) for tag in [*ep.tags, *ep.llm_tag_candidates])
    dispatch.answer(anchor_p=0.9)
    second = runner.run([ep], _ctx(dispatch))
    rows = _tag_judgments(ep)
    assert second.counts["judgments_appended"] == 4
    by_model = {(row["judge_role"], row["judge_model"]) for row in rows}
    assert ("sibling", "google/gemma-4-31b-it") in by_model  # the rule candidate
    # The Gemini-produced LLM tag is judged by the non-Google sibling.
    llm_rows = ledger.judgments(ep.llm_tag_candidates[0])
    assert {r["judge_model"] for r in llm_rows if r["judge_role"] == "sibling"} == {
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    }
    assert ep.uid in second.episodes_complete
    # Shadow: nothing but judgments changed on any candidate.
    for after, original in zip(
        [*ep.tags, *ep.llm_tag_candidates], [*before.tags, *before.llm_tag_candidates], strict=True
    ):
        stripped = {k: v for k, v in after.items() if k not in ("judgments", "judge_pending")}
        assert stripped == original
    # A third pass has nothing to do.
    third = runner.run([ep], _ctx(dispatch))
    assert third.counts["items_submitted"] == 0


def test_the_anchor_escalates_to_t2_only_in_the_band():
    dispatch = FakeDispatch()
    ep = _episode(llm=[])
    runner.run([ep], _ctx(dispatch))
    dispatch.answer(anchor_p=0.5)
    runner.run([ep], _ctx(dispatch))
    escalated = [
        item
        for packet, _ in dispatch.submitted.values()
        for item in packet.items
        if item.tier == "T2"
    ]
    assert escalated and {item.sample for item in escalated} == {"escalation"}
    assert all(p.role == "anchor" for p, _ in dispatch.submitted.values())

    calm = FakeDispatch()
    ep2 = _episode(llm=[])
    runner.run([ep2], _ctx(calm))
    calm.answer(anchor_p=0.95)
    runner.run([ep2], _ctx(calm))
    assert not calm.submitted


def test_a_failed_or_lost_packet_writes_nothing_and_is_replanned():
    dispatch = FakeDispatch()
    dispatch.fail = True
    ep = _episode()
    result = runner.run([ep], _ctx(dispatch))
    assert result.counts["errored"] == 3 and not _tag_judgments(ep)
    assert not any(ledger.pending(t) for t in [*ep.tags, *ep.llm_tag_candidates])
    # A submitted packet whose record disappears (cancelled, structurally blocked) is re-planned.
    dispatch.fail = False
    runner.run([ep], _ctx(dispatch))
    dispatch.records.clear()
    again = runner.run([ep], _ctx(dispatch))
    assert again.counts["replanned"] >= 3 and again.counts["items_submitted"] >= 3


def test_run_caps_and_stop_are_honoured():
    dispatch = FakeDispatch()
    ep = _episode()
    capped = runner.run([ep], _ctx(dispatch, run_caps={"judge:sibling": 0}))
    assert capped.counts["packets:sibling"] == 0 and capped.counts["packets:anchor"] == 1
    stopped = runner.run([_episode(uid="ep-2")], _ctx(FakeDispatch(), stop=lambda: True))
    assert stopped.counts["stopped"] == 1 and stopped.counts["items_submitted"] == 0


def test_moments_are_judged_per_meeting_with_both_choose_orders():
    dispatch = FakeDispatch()
    moments = [_moment("We will fix the bridge", 600), _moment("Taxes will not rise", 900)]
    ep = _episode(tags=[], llm=[], moments=moments)
    runner.run([ep], _ctx(dispatch, tasks=("moment",)))
    anchor_packets = [p for p, _ in dispatch.submitted.values() if p.role == "anchor"]
    [packet] = anchor_packets
    question_ids = sorted({item.question_id for item in packet.items})
    assert question_ids == ["best", "best_r", "publishable", "supported", "usefulness"]
    # Gemini-produced moments get the non-Google sibling for every question, choose included.
    assert {p.judge_model for p, _ in dispatch.submitted.values() if p.role == "sibling"} == {
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    }
    dispatch.answer()
    runner.run([ep], _ctx(dispatch, tasks=("moment",)))
    best = [r for m in moments for r in ledger.judgments(m) if r["question_id"] == "best"]
    assert len(best) == 4  # two subjects x (anchor, sibling)


def test_systemone_jobs_dispatch_their_questions_not_messages():
    from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig, _messages

    packet = Packet(
        "judge:anchor",
        "anchor",
        "typesafe/jev-1.13",
        (
            Item(
                QuestionSpec("supported", "validate", "x"),
                "T1",
                make_evidence("T1", "e"),
                (Subject("tag", "s", "ep", None, None, {}),),
            ),
        ),
    )
    job = JevBackend().build(packet)
    assert json.loads(_messages(job)[0]["content"])["questions"]  # estimate stand-in only
    backend = LiteLLMBackend(
        LLMBackendConfig(
            model="typesafe/jev-1.13", mode="dispatch", dispatch_v2_url="https://dispatch.test"
        )
    )
    payload = backend._payload(job, resolved_model="typesafe/jev-1.13")
    assert payload["systemone"] == job.inputs["systemone"] and "messages" not in payload
    with pytest.raises(Exception, match="only be dispatched to the Worker"):
        backend._payload(job, resolved_model="typesafe/jev-1.13", direct=True)


# ---- stage -------------------------------------------------------------------------------------


class _Storage:
    def __init__(self, files):
        self.files = files

    def exists(self, key):
        return key in self.files

    def get_file(self, key, path):
        if key not in self.files:
            return False
        Path(path).write_bytes(self.files[key])
        return True


class _Backend:
    def __init__(self, storage):
        self.storage = storage
        self.jobs = []
        self.records = {}  # the deferred registry: recipe -> pending handle

    def run_inference(self, job):
        self.jobs.append(job)
        handle = JobHandle(task="judge", recipe_hash=job.recipe_hash, backend="v2", ref="r")
        self.records[job.recipe_hash] = handle
        return handle


def _vtt(segments):
    def ts(seconds):
        h, rem = divmod(int(seconds), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}.000"

    return (
        "WEBVTT\n\n"
        + "\n\n".join(f"{ts(r['start'])} --> {ts(r['end'])}\n{r['text']}" for r in segments)
        + "\n"
    ).encode()


def _stage_ctx(enabled):
    from citypods.stages import StageContext

    storage = _Storage({"t.vtt": _vtt(_segments())})
    return StageContext(
        storage=storage,
        ffmpeg="ffmpeg",
        max_kbps=64,
        dry_run=False,
        judge_backend=_Backend(storage),
        judging_config={"enabled": enabled, "tasks": {"tag": {"mode": "shadow"}}},
    )


def test_the_judge_stage_does_nothing_until_enabled():
    from citypods.stages import JudgeStage

    ctx = _stage_ctx(enabled=False)
    ep = _episode()
    ep.transcript_key, ep.transcript_format = "t.vtt", "vtt"
    JudgeStage().process(None, SimpleNamespace(slug="x"), [ep], ctx)
    assert ctx.judge_backend.jobs == [] and not _tag_judgments(ep)
    assert JudgeStage().telemetry_purposes(ctx) == ()


def test_the_judge_stage_submits_shadow_packets_and_records_markers():
    from citypods.stages import JudgeStage

    ctx = _stage_ctx(enabled=True)
    ep = _episode()
    ep.transcript_key, ep.transcript_format = "t.vtt", "vtt"
    JudgeStage().process(None, SimpleNamespace(slug="x"), [ep], ctx)
    purposes = sorted(job.inputs["llm_policy"].purpose for job in ctx.judge_backend.jobs)
    assert purposes == ["judge:anchor", "judge:sibling", "judge:sibling"]
    assert all(ledger.pending(tag) for tag in [*ep.tags, *ep.llm_tag_candidates])
    assert ep.tags[0]["display"] is True and ep.llm_tag_candidates[0]["display"] is False


def test_the_report_reads_agreement_escalation_and_backfill_from_stored_judgments():
    from datetime import UTC, datetime

    from citypods.judging.report import summarize

    def row(role, value, tier="T1", sample="routine"):
        return {
            "judge_role": role,
            "kind": "validate",
            "question_id": "supported",
            "context_tier": tier,
            "value": value,
            "sample": sample,
            "judged_at": "2026-10-09T00:00:00+00:00",
        }

    agree = {"id": "a", "judgments": [row("anchor", 0.9), row("sibling", True)]}
    disagree = {
        "id": "b",
        "judgments": [
            row("anchor", 0.4),
            row("sibling", True),
            row("anchor", 0.8, "T2", "escalation"),
        ],
    }
    untouched = {"id": "c"}
    report = summarize(
        [{"published": "2026-10-01", "tags": [agree, disagree, untouched]}],
        now=datetime(2026, 10, 9, tzinfo=UTC),
    )
    assert report["agreement"]["tag:supported@T1"] == {"agree": 1, "pairs": 2, "rate": 0.5}
    assert report["escalation_rate"] == 0.5
    assert report["backfill_last_90_days"] == {
        "candidates": 3,
        "judged_by_both_at_first_tier": 2,
        "share": round(2 / 3, 4),
    }


# ---- dirtiness (the judge stage must be revisited while answers are pending) -------------------


def _city():
    return SimpleNamespace(slug="x", provider="granicus", extra={})


def test_the_judge_stage_is_dirty_only_while_it_has_work():
    from citypods.judging.runner import episode_needs_judging
    from citypods.stages import judge_episode_dirty

    config = {"enabled": True, "tasks": {"tag": {"mode": "shadow"}}, "all_tiers_sample_rate": 0.0}
    ep = _episode()
    ep.transcript_key = "t.vtt"
    assert judge_episode_dirty(ep, config)
    assert not judge_episode_dirty(ep, {**config, "enabled": False})
    no_transcript = _episode()
    no_transcript.transcript_key = None
    assert not judge_episode_dirty(no_transcript, config)

    dispatch = FakeDispatch()
    runner.run([ep], _ctx(dispatch))
    assert judge_episode_dirty(ep, config)  # answers pending
    dispatch.answer(anchor_p=0.95)
    runner.run([ep], _ctx(dispatch))
    assert not judge_episode_dirty(ep, config)  # everything judged, nothing in flight
    kwargs = {
        "anchor": LANES["judge:anchor"],
        "sibling": LANES["judge:sibling"],
        "families": FAMILIES,
        "all_tiers_sample_rate": 0.0,
    }
    assert not episode_needs_judging(ep, [task("tag")], **kwargs)
    # A new candidate on the same episode makes it dirty again.
    ep.tags.append(_rule_tag(id="new-tag"))
    assert episode_needs_judging(ep, [task("tag")], **kwargs)


def test_two_run_stages_passes_submit_then_keep_collecting(monkeypatch):
    """Integration (CodeRabbit, 2026-10-09): the stage runs again on the next pass, never cached."""
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.stages import JudgeStage, run_stages

    ep = Episode(
        guid="ep-1",
        title="Council",
        published=datetime(2026, 10, 1, tzinfo=UTC),
        video_url="https://example.test/v",
        uid="ep-1",
    )
    ep.tags, ep.llm_tag_candidates = [_rule_tag()], [_llm_tag()]
    ep.chapters = [{"start": 0, "title": "Rezoning"}, {"start": 1200, "title": "Budget"}]
    ep.transcript_key, ep.transcript_format = "t.vtt", "vtt"
    disabled = _stage_ctx(enabled=False)
    disabled.lane = "judge"
    run_stages(None, _city(), [ep], [JudgeStage()], disabled, quiet=True)
    assert disabled.judge_backend.jobs == []

    ctx = _stage_ctx(enabled=True)
    ctx.lane = "judge"
    monkeypatch.setattr(
        "citypods.compute.llm_deferred.look_up_deferred",
        lambda storage, recipe: ctx.judge_backend.records.get(recipe),
    )
    run_stages(None, _city(), [ep], [JudgeStage()], ctx, quiet=True)
    first = len(ctx.judge_backend.jobs)
    assert first == 3 and all(ledger.pending(t) for t in [*ep.tags, *ep.llm_tag_candidates])
    # The second pass must visit the episode again (answers are pending), not skip it as done.
    assert judge_dirty(ep)
    for job in ctx.judge_backend.jobs:
        ctx.judge_backend.records[job.recipe_hash] = JobResult(
            task="judge", recipe_hash=job.recipe_hash, output=answer_job(job, anchor_p=0.95)
        )
    run_stages(None, _city(), [ep], [JudgeStage()], ctx, quiet=True)
    # Visited again: the answers became judgments and nothing new was submitted.
    assert len(_tag_judgments(ep)) == 4 and len(ctx.judge_backend.jobs) == first
    assert not judge_dirty(ep)


def judge_dirty(ep):
    from citypods.stages import judge_episode_dirty

    return judge_episode_dirty(ep, {"enabled": True, "tasks": {"tag": {"mode": "shadow"}}})
