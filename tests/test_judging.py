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
    # The canonical tag ledger (llm_tag_candidates) holds rule and LLM candidates alike; ``tags``
    # is the projected visible list derived from it and is never read by the judge.
    rule = tags if tags is not None else [_rule_tag()]
    llm = llm if llm is not None else [_llm_tag()]
    return SimpleNamespace(
        uid=uid,
        guid=uid,
        published=published,
        tags=rule,
        llm_tag_candidates=[*rule, *llm],
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
    assert family_of("unknown/model", FAMILIES) == "unknown"


# ---- packing -----------------------------------------------------------------------------------


def _item(subject_id, tokens, kind="validate"):
    subject = Subject("tag", subject_id, "ep", None, None, {})
    return Item(
        QuestionSpec("q", kind, "x"), "T1", make_evidence("T1", "w" * 4 * tokens), (subject,)
    )


def test_packing_respects_ceilings_and_skips_only_an_item_too_large_alone():
    backend = ChatJudgeBackend(max_total_tokens=1000, max_items=3)
    units = [[_item("a", 400)], [_item("b", 400)], [_item("c", 400)], [_item("d", 5000)]]
    units.append([_item("e", 100), _item("f", 100), _item("g", 100)])
    result = pack(units, backend, purpose="judge:sibling", role="sibling", judge_model="m")
    sizes = [[i.subjects[0].subject_id for i in p.items] for p in result.packets]
    assert sizes == [["a", "b"], ["c"], ["e", "f", "g"]]
    assert result.payload_too_large == 1
    again = pack(units, backend, purpose="judge:sibling", role="sibling", judge_model="m")
    assert [p.recipe_hash for p in again.packets] == [p.recipe_hash for p in result.packets]


def _meeting_unit(candidates, tokens=100):
    subjects = [Subject("moment", f"m{i}", "ep", None, None, {}) for i in range(candidates)]
    unit = []
    for subject in subjects:
        for kind in ("validate", "gate", "grade"):
            unit.append(
                Item(
                    QuestionSpec(kind, kind, "x"),
                    "T0",
                    make_evidence("T0", "w" * 4 * tokens),
                    (subject,),
                )
            )
    for order in ("", "_r"):
        unit.append(
            Item(
                QuestionSpec("choose", "choose", "x"),
                "T0",
                make_evidence("T0", "w" * 4 * tokens),
                tuple(subjects),
                order=order,
            )
        )
    return unit


def test_an_oversize_meeting_is_split_by_candidate_not_skipped():
    """review/53 PR4 dry run: a meeting with more than 7 candidates exceeded a 25-item sibling
    packet and lost every judgment (481 units). It is now split: both choose items together, each
    candidate's three items together, nothing skipped."""
    backend = ChatJudgeBackend(max_total_tokens=10_000, max_items=7)
    unit = _meeting_unit(4)  # 14 items > 7
    result = pack([unit], backend, purpose="judge:sibling", role="sibling", judge_model="m")
    assert result.payload_too_large == 0 and result.units_split == 1
    packed = [item for packet in result.packets for item in packet.items]
    assert sorted(map(id, packed)) == sorted(map(id, unit))
    for packet in result.packets:
        kinds = [item.question.kind for item in packet.items]
        assert len(packet.items) <= 7
        if "choose" in kinds:
            assert kinds.count("choose") == 2
    for subject_id in ("m0", "m1", "m2", "m3"):
        homes = {
            index
            for index, packet in enumerate(result.packets)
            for item in packet.items
            if item.question.kind != "choose" and item.subjects[0].subject_id == subject_id
        }
        assert len(homes) == 1  # a candidate's items share one packet
    # A meeting that fits is still one packet, unsplit.
    whole = pack([_meeting_unit(1)], backend, purpose="p", role="sibling", judge_model="m")
    assert len(whole.packets) == 1 and whole.units_split == 0


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


def test_ledger_is_append_only_deduplicated_and_outside_the_candidate():
    candidate = {"id": "zoning-reform"}
    store = {}
    subject = Subject("tag", "s1", "ep", None, None, candidate, store)
    row = {k: "x" for k in ledger.KEY_FIELDS}
    assert ledger.append_judgment(subject, row)
    assert not ledger.append_judgment(subject, dict(row))
    assert len(ledger.judgments(subject)) == 1
    ledger.add_pending(subject, {**row, "evidence_digest": "y"})
    ledger.add_pending(subject, {**row, "evidence_digest": "y"})
    assert len(ledger.pending(subject)) == 1
    ledger.drop_pending(subject, {**row, "evidence_digest": "y"})
    assert "pending" not in store["subjects"]["s1"]
    # Judgments live in the episode's judging block, never in the candidate other lanes own.
    assert candidate == {"id": "zoning-reform"}
    ledger.mark_unbuildable(subject, "q|m|1|T2", "t.vtt:abc")
    assert ledger.unbuildable(subject) == {"q|m|1|T2": "t.vtt:abc"}


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

    def answer(self, anchor_p=0.9, sibling=True, *, verdicts=None, bad_quotes=()):
        """Answer every submitted packet; ``verdicts`` overrides a chat role's verdict and
        ``bad_quotes`` names roles whose reasons quote text that is not in the evidence."""
        for recipe, (packet, job) in self.submitted.items():
            verdict = (verdicts or {}).get(packet.role, sibling)
            output = answer_job(
                job, anchor_p=anchor_p, sibling=verdict, bad_quote=packet.role in bad_quotes
            )
            self.records[recipe] = JobResult(task="judge", recipe_hash=recipe, output=output)
        self.submitted.clear()


def _quoted(item_text, *, bad=False):
    """A reason quoting the evidence word for word (or text that appears nowhere in it)."""
    if bad:
        return '"words that appear nowhere in this evidence"'
    tail = item_text
    for marker in ("Transcript:", "Quote: ", "A: "):
        if marker in item_text:
            tail = item_text.split(marker, 1)[1]
            break
    return '"' + " ".join(tail.split()[:5]) + '"'


def answer_job(job, *, anchor_p=0.9, sibling=True, bad_quote=False):
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
        answer = {"id": entry["id"], "reason": _quoted(entry["item"], bad=bad_quote)}
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


def _subjects(ep, name="tag"):
    return task(name).subjects(ep)


def _tag_judgments(ep):
    return [row for subject in _subjects(ep) for row in ledger.judgments(subject)]


def _all_pending(ep, name="tag"):
    return all(ledger.pending(subject) for subject in _subjects(ep, name))


def _any_pending(ep, name="tag"):
    return any(ledger.pending(subject) for subject in _subjects(ep, name))


def test_a_judge_pass_records_anchor_and_independent_sibling_judgments():
    dispatch = FakeDispatch()
    ep = _episode()
    before = copy.deepcopy(ep)
    first = runner.run([ep], _ctx(dispatch))
    assert first.counts["packets:anchor"] == 1 and first.counts["packets:sibling"] == 2
    assert _all_pending(ep)
    dispatch.answer(anchor_p=0.9)
    second = runner.run([ep], _ctx(dispatch))
    rows = _tag_judgments(ep)
    assert second.counts["judgments_appended"] == 4
    by_model = {(row["judge_role"], row["judge_model"]) for row in rows}
    assert ("sibling", "google/gemma-4-31b-it") in by_model  # the rule candidate
    # The Gemini-produced LLM tag is judged by the non-Google sibling.
    [llm_subject] = [x for x in _subjects(ep) if x.payload.get("source_kind") == "llm"]
    llm_rows = ledger.judgments(llm_subject)
    assert {r["judge_model"] for r in llm_rows if r["judge_role"] == "sibling"} == {
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    }
    assert ep.uid in second.episodes_complete
    # Shadow: every candidate is byte-for-byte unchanged; judgments live in ep.judging only.
    assert ep.tags == before.tags and ep.llm_tag_candidates == before.llm_tag_candidates
    assert set(ep.judging) == {"subjects"}
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
    assert not _any_pending(ep)
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
    # A stop that is already set ends the pass while planning, before any transcript is read.
    assert stopped.counts["plan_stopped"] == 1 and stopped.counts["items_submitted"] == 0


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
    best = [
        r
        for x in _subjects(ep, "moment")
        for r in ledger.judgments(x)
        if r["question_id"] == "best"
    ]
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
    assert _all_pending(ep)
    assert ep.tags[0]["display"] is True and ep.llm_tag_candidates[-1]["display"] is False


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

    judging = {
        "subjects": {
            "a": {"task": "tag", "judgments": [row("anchor", 0.9), row("sibling", True)]},
            "b": {
                "task": "tag",
                "judgments": [
                    row("anchor", 0.4),
                    row("sibling", True),
                    row("anchor", 0.8, "T2", "escalation"),
                ],
            },
        }
    }
    record = {
        "published": "2026-10-01",
        "llm_tag_candidates": [
            {"id": "a"},
            {"id": "b"},
            {"id": "c"},
            {"id": "d", "candidate_state": "historical"},
        ],
        "judging": judging,
    }
    report = summarize([record], now=datetime(2026, 10, 9, tzinfo=UTC))
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
    ep.llm_tag_candidates.append(_rule_tag(id="new-tag"))
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
    ep.tags = [_rule_tag()]
    ep.llm_tag_candidates = [*ep.tags, _llm_tag()]
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
    assert first == 3 and _all_pending(ep)
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


# ---- record ownership, unbuildable evidence, choose completeness, shared caps ----------------


def test_judgments_are_their_own_record_block_owned_only_by_the_judge_lane():
    from datetime import UTC, datetime

    from citypods.models import Episode
    from citypods.records import episode_to_record, protected_blocks_for_lane, record_to_episode
    from citypods.run import _RECORD_BACKED_LANES, _SOURCE_STAGE_NAMES

    ep = Episode(
        guid="g",
        title="t",
        published=datetime(2026, 10, 1, tzinfo=UTC),
        video_url="https://example.test/v",
        uid="u",
    )
    ep.judging = {"subjects": {"s": {"task": "tag", "judgments": [{"value": 0.9}]}}}
    assert record_to_episode(episode_to_record(ep)).judging == ep.judging
    # The judge lane writes only its own block; the tag/moments lanes never write it.
    assert "judging" not in protected_blocks_for_lane("judge")
    assert {"tags", "llm_tag_candidates", "moments", "audio"} <= protected_blocks_for_lane("judge")
    assert "judging" in protected_blocks_for_lane("tag")
    assert "judging" in protected_blocks_for_lane("moments")
    # Source-scoped (packets span a source's episodes; caps spent once per run), record-backed.
    assert "judge" in _SOURCE_STAGE_NAMES and "judge" in _RECORD_BACKED_LANES


def test_unbuildable_evidence_is_not_revisited_until_the_transcript_changes():
    from citypods.judging.runner import episode_needs_judging

    no_anchor = _rule_tag(evidence=[{"where": "agenda", "span": "rezoning"}], chapter_id=None)
    ep = _episode(tags=[no_anchor], llm=[])
    ep.transcript_key, ep.transcript_spec_hash = "t.vtt", "v1"
    texts = EpisodeTexts(segments=_segments(), chapters=(), identity="t.vtt:v1")
    dispatch = FakeDispatch()
    ctx = _ctx(dispatch)
    ctx.texts_for = lambda _ep: texts
    runner.run([ep], ctx)  # T1 has no time to centre on and no chapter: unbuildable
    kwargs = {
        "anchor": LANES["judge:anchor"],
        "sibling": LANES["judge:sibling"],
        "families": FAMILIES,
        "all_tiers_sample_rate": 0.0,
    }
    assert not dispatch.submitted
    assert not episode_needs_judging(ep, [task("tag")], **kwargs)
    ep.transcript_spec_hash = "v2"  # a new transcript re-opens it
    assert episode_needs_judging(ep, [task("tag")], **kwargs)


def test_a_meeting_is_not_complete_until_the_sibling_has_chosen_too():
    from citypods.judging.runner import episode_needs_judging

    moments = [_moment("We will fix the bridge", 600), _moment("Taxes will not rise", 900)]
    ep = _episode(tags=[], llm=[], moments=moments)
    ep.transcript_key = "t.vtt"
    dispatch = FakeDispatch()
    runner.run([ep], _ctx(dispatch, tasks=("moment",)))
    dispatch.answer()
    runner.run([ep], _ctx(dispatch, tasks=("moment",)))
    kwargs = {
        "anchor": LANES["judge:anchor"],
        "sibling": LANES["judge:sibling"],
        "families": FAMILIES,
        "all_tiers_sample_rate": 0.0,
    }
    assert not episode_needs_judging(ep, [task("moment")], **kwargs)
    # Drop the sibling's choose answers: the meeting is incomplete again.
    first = task("moment").subjects(ep)[0]
    entry = ep.judging["subjects"][first.subject_id]
    entry["judgments"] = [
        r
        for r in entry["judgments"]
        if not (r["judge_role"] == "sibling" and r["question_id"].startswith("best"))
    ]
    assert episode_needs_judging(ep, [task("moment")], **kwargs)


def test_run_caps_are_shared_across_sources_in_one_run():
    import threading

    caps = {"judge:anchor": 1, "judge:sibling": 100}
    lock = threading.Lock()
    first = runner.run(
        [_episode(uid="a", llm=[])], _ctx(FakeDispatch(), run_caps=caps, run_caps_lock=lock)
    )
    second = runner.run(
        [_episode(uid="b", llm=[])], _ctx(FakeDispatch(), run_caps=caps, run_caps_lock=lock)
    )
    assert first.counts["packets:anchor"] == 1 and second.counts["packets:anchor"] == 0
    assert second.counts["run_cap:judge:anchor"] == 1 and caps["judge:anchor"] == 0


def test_stored_producer_identities_are_normalized_and_unknown_ones_fail_safe():
    sibling = LANES["judge:sibling"]
    # Tag LLM candidates record the backend-prefixed form (stages.py TagsStage).
    prefixed = "litellm:" + GEMINI
    assert family_of(prefixed, FAMILIES) == "google"
    assert sibling_for(prefixed, sibling, FAMILIES) == (
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    )
    # Route-specific aliases resolve to the logical key.
    assert family_of("nvidia/deepseek-v4.1-flash", FAMILIES) == "deepseek"
    # An unknown producer is never paired with a possibly same-family judge.
    assert family_of("acme/new-model", FAMILIES) == "unknown"
    assert sibling_for("acme/new-model", sibling, FAMILIES) is None
    ep = _episode(tags=[], llm=[_llm_tag(provider_model="acme/new-model")])
    result = runner.run([ep], _ctx(FakeDispatch()))
    assert result.counts["unknown_producer_family"] == 1
    assert result.counts["packets:anchor"] == 1 and result.counts["packets:sibling"] == 0


def test_tag_subjects_come_from_the_ledger_once_and_skip_historical_rows():
    rule = _rule_tag()
    historical = _llm_tag(candidate_state="historical", id="old-tag")
    ep = _episode(tags=[rule], llm=[historical])
    ep.llm_tag_candidates.append(dict(rule))  # a duplicate row of the same subject
    subjects = task("tag").subjects(ep)
    assert [x.payload["id"] for x in subjects] == ["zoning-reform"]


def test_an_unreadable_transcript_skips_that_episode_and_keeps_it_incomplete():
    """The first PR4 dry run died on one transcript whose download exhausted its retries. That
    episode is skipped for the pass (counted, not marked unbuildable, not complete) and every other
    episode is still judged."""
    dispatch = FakeDispatch()
    broken, healthy = _episode(uid="ep-broken"), _episode(uid="ep-ok", published="2026-09-30")

    def texts_for(ep):
        if ep.uid == "ep-broken":
            raise runner.TextsUnavailable("storage read unavailable")
        return TEXTS

    ctx = _ctx(dispatch)
    ctx.texts_for = texts_for
    stats = runner.run([broken, healthy], ctx)
    assert stats.counts["texts_unavailable"] == 1
    assert "ep-broken" in stats.episodes_unavailable
    assert "ep-broken" not in stats.episodes_complete
    assert _all_pending(healthy) and not _any_pending(broken)
    assert not any(ledger.unbuildable(s) for s in _subjects(broken))


def test_a_pass_restores_the_contexts_texts_reader():
    """The per-pass texts cache must not outlive the pass, or a reused context could never retry
    a read that failed once."""
    calls = []

    def texts_for(ep):
        calls.append(ep.uid)
        raise runner.TextsUnavailable("down")

    ctx = _ctx(FakeDispatch())
    ctx.texts_for = texts_for
    ep = _episode()
    runner.run([ep], ctx)
    assert ctx.texts_for is texts_for
    runner.run([ep], ctx)
    assert calls == [ep.uid, ep.uid]


def test_planning_stops_at_the_item_budget_and_reads_no_further_transcripts(monkeypatch):
    """A cold backlog: the first PR4 dry run read every candidate's transcript before submitting
    anything and overran its window. Planning now stops once a pass's worth of items is planned;
    later episodes are not read, not complete, and stay dirty for the next pass."""
    dispatch = FakeDispatch()
    episodes = [_episode(uid=f"ep-{i}", published=f"2026-10-0{i + 1}") for i in range(4)]
    read = []

    def texts_for(ep):
        read.append(ep.uid)
        return TEXTS

    monkeypatch.setattr(
        runner, "_item_budget", lambda _ctx: {"judge:anchor": 1, "judge:sibling": 1}
    )
    ctx = _ctx(dispatch)
    ctx.texts_for = texts_for
    stats = runner.run(episodes, ctx)
    assert read == ["ep-3"]  # newest first; one episode fills both budgets
    assert stats.counts["plan_budget_reached"] == 1
    assert stats.episodes_planned == {"ep-3"}
    assert stats.episodes_complete == set()
    assert _all_pending(episodes[3]) and not _any_pending(episodes[0])

    read.clear()
    ctx = _ctx(FakeDispatch())
    ctx.texts_for = texts_for
    ctx.stop = lambda: bool(read)
    stats = runner.run([_episode(uid=f"s-{i}") for i in range(3)], ctx)
    assert len(read) == 1 and stats.counts["plan_stopped"] == 1


def test_the_item_budget_is_run_cap_packets_times_the_fullest_packet():
    ctx = _ctx(FakeDispatch(), run_caps={"judge:anchor": 2, "judge:sibling": 3})
    budget = runner._item_budget(ctx)
    sibling_items = max(runner._sibling_backend(m).max_items for m in ctx.sibling.models)
    assert budget == {"judge:anchor": 120, "judge:sibling": 3 * sibling_items}
    assert runner._item_budget(_ctx(FakeDispatch())) is None  # uncapped: no bound


def test_a_dry_run_rehearses_one_capped_pass_and_submits_nothing():
    """The first full dry run ignored the run caps, so every source planned and counted its whole
    backlog. It now spends the shared caps like a real pass and still submits nothing."""
    dispatch = FakeDispatch()
    ctx = _ctx(dispatch, run_caps={"judge:anchor": 1, "judge:sibling": 1}, dry_run=True)
    stats = runner.run([_episode(uid=f"ep-{i}") for i in range(3)], ctx)
    assert not dispatch.submitted
    assert stats.counts["dry_run_packets:anchor"] == 1
    assert stats.counts["dry_run_packets:sibling"] == 1
    assert ctx.run_caps == {"judge:anchor": 0, "judge:sibling": 0}


def test_one_judge_pass_across_sources_gives_the_caps_to_the_newest_episodes(monkeypatch):
    """The capped PR4 dry run judged source by source, so the alphabetically first sources spent
    every run cap. run_judge_across_sources plans every source's episodes in one newest-first pass:
    with a budget for one episode, the newest wins even in the source that sorts last."""
    from citypods.stages import JudgeStage, run_judge_across_sources

    monkeypatch.setattr(
        runner, "_item_budget", lambda _ctx: {"judge:anchor": 1, "judge:sibling": 1}
    )
    ctx = _stage_ctx(enabled=True)
    old = _episode(uid="addison-old", published="2025-01-01")
    new = _episode(uid="waco-new", published="2026-10-09")
    for ep in (old, new):
        ep.transcript_key, ep.transcript_format = "t.vtt", "vtt"
    stat = run_judge_across_sources(
        JudgeStage(),
        [(SimpleNamespace(slug="addison"), [old]), (SimpleNamespace(slug="waco"), [new])],
        ctx,
    )
    assert _any_pending(new) and not _any_pending(old)
    assert stat.ran == 0 and ctx.judge_backend.jobs


def _wire_tokens(backend, packet):
    """What the producer estimates for the job: chars/4 of the serialized request."""
    from citypods.judging.backends import estimate_tokens

    return estimate_tokens(json.dumps(backend.build(packet).inputs["systemone"], sort_keys=True))


def _escaped_item(subject_id, size):
    """An item whose JSON form is much larger than its text: quotes and newlines escape."""
    text = ('he said "yes"\nthen left\n' * size)[: size * 4]
    subject = Subject("tag", subject_id, "ep", None, None, {})
    return Item(
        QuestionSpec("q", "validate", "Is it?"), "T1", make_evidence("T1", text), (subject,)
    )


@pytest.mark.parametrize("ratio", [None, 1.0, 1.15, 1.4])
def test_jev_packets_fit_the_route_ceiling_as_the_producer_estimates_them(ratio):
    """The first PR4 runs sized packets by item text alone; the JSON the producer sends is larger,
    so about 45 of 110 anchor packets a run were failed by the Worker as over the input ceiling."""
    import math

    from citypods.judging.backends import JEV_HARD_INPUT_CEILING, JEV_UNMEASURED_RATIO

    backend = JevBackend(ratio)
    units = [[_escaped_item(f"s{i}", 900)] for i in range(120)]
    result = pack(units, backend, purpose="judge:anchor", role="anchor", judge_model="jev")
    assert result.packets and result.payload_too_large == 0
    effective = max(1.0, ratio) if ratio else JEV_UNMEASURED_RATIO
    for packet in result.packets:
        scaled = math.ceil(_wire_tokens(backend, packet) * effective)
        assert scaled <= JEV_HARD_INPUT_CEILING, (len(packet.items), scaled)
    # The text-only measure the runs used would have let packets through that exceed it.
    text_backend = type(
        "TextOnly",
        (),
        {
            "max_total_tokens": JEV_HARD_INPUT_CEILING,
            "max_items": 60,
            "max_question_tokens": 28_000,
        },
    )()
    old = pack(units, text_backend, purpose="judge:anchor", role="anchor", judge_model="jev")
    assert any(_wire_tokens(JevBackend(1.0), p) > JEV_HARD_INPUT_CEILING for p in old.packets)


def test_a_higher_learned_ratio_makes_smaller_jev_packets():
    assert JevBackend(1.4).max_total_tokens < JevBackend(1.0).max_total_tokens
    # A ratio below 1 never enlarges a packet past the ceiling, and an unreadable one errs small.
    assert JevBackend(0.5).max_total_tokens == JevBackend(1.0).max_total_tokens
    assert JevBackend(None).max_total_tokens < JevBackend(1.0).max_total_tokens


def test_the_jev_sizing_constants_match_the_route_catalog():
    import yaml

    from citypods.judging.backends import JEV_HARD_INPUT_CEILING, JEV_ROUTE_ID

    catalog = yaml.safe_load(
        (Path(__file__).parent.parent / "config" / "provider_limits.yml").read_text()
    )
    routes = [r for r in catalog["routes"] if r.get("route_id") == JEV_ROUTE_ID]
    assert len(routes) == 1
    assert routes[0]["hard_input_ceiling"] == JEV_HARD_INPUT_CEILING


def test_a_pass_submits_only_into_the_room_left_under_the_outstanding_limit():
    """The PR4 shadow run queued ~8,500 jobs and added ~3,300 a day against ~700 claimed."""
    dispatch = FakeDispatch()
    limits = {"judge:anchor": 1, "judge:sibling": 1}
    first_ep, second_ep = _episode(uid="ep-a"), _episode(uid="ep-b")
    first = runner.run([first_ep], _ctx(dispatch, max_outstanding=limits))
    assert first.counts["packets:anchor"] == 1 and first.counts["packets:sibling"] == 1
    assert first.counts["outstanding:judge:anchor"] == 0
    held = runner.run([first_ep, second_ep], _ctx(dispatch, max_outstanding=limits))
    assert held.counts["packets:anchor"] == 0 and held.counts["packets:sibling"] == 0
    assert held.counts["outstanding:judge:anchor"] == 1
    assert held.counts["run_cap_outstanding:judge:anchor"] == 1
    assert not _any_pending(second_ep)
    dispatch.answer()
    freed = runner.run([first_ep, second_ep], _ctx(dispatch, max_outstanding=limits))
    assert freed.counts["packets:anchor"] == 1 and _any_pending(second_ep)


def test_the_outstanding_limit_never_raises_a_tighter_run_cap():
    dispatch = FakeDispatch()
    caps = {"judge:anchor": 0, "judge:sibling": 5}
    ctx = _ctx(dispatch, run_caps=caps, max_outstanding={"judge:anchor": 50, "judge:sibling": 2})
    stats = runner.run([_episode()], ctx)
    assert stats.counts["packets:anchor"] == 0
    assert caps["judge:anchor"] == 0 and caps["judge:sibling"] <= 2


def test_the_judge_stage_hands_the_runner_its_ratio_and_outstanding_limits(monkeypatch):
    from citypods import stages
    from citypods.judging.backends import JEV_ROUTE_ID

    seen = {}

    def spy(episodes, judging):
        seen["ctx"] = judging
        return runner.JudgingStats()

    monkeypatch.setattr(runner, "run", spy)
    asked = []

    def learned(route_id, family, *, backend=None):
        asked.append((route_id, family))
        return 1.17

    monkeypatch.setattr("citypods.compute.llm.dispatch_v2_learned_input_ratio", learned)
    ctx = _stage_ctx(enabled=True)
    ctx.judging_config["max_outstanding_packets"] = {"anchor": 7, "sibling": 9}
    ep = _episode()
    ep.transcript_key, ep.transcript_format = "t.vtt", "vtt"
    stages.JudgeStage().process(None, SimpleNamespace(slug="x"), [ep], ctx)
    assert asked == [(JEV_ROUTE_ID, "judge")]
    assert seen["ctx"].anchor_input_ratio == 1.17
    assert seen["ctx"].max_outstanding == {"judge:anchor": 7, "judge:sibling": 9}


def test_packets_waiting_for_superseded_subjects_still_count_as_outstanding():
    """A candidate whose evidence changes gets a new subject id, so its old pending markers are
    never visited while the packet they name is still queued; they must still hold the limit."""
    dispatch = FakeDispatch()
    limits = {"judge:anchor": 1, "judge:sibling": 1}
    ep = _episode(uid="ep-a")
    first = runner.run([ep], _ctx(dispatch, max_outstanding=limits))
    assert first.counts["packets:anchor"] == 1
    store = _subjects(ep)[0].store
    # Supersede every subject: the entries (and their pending markers) stay, under ids no
    # current subject has.
    store["subjects"] = {f"old-{key}": entry for key, entry in store["subjects"].items()}
    held = runner.run([ep], _ctx(dispatch, max_outstanding=limits))
    assert held.counts["superseded_pending"] >= 1
    assert held.counts["outstanding:judge:anchor"] == 1
    assert held.counts["packets:anchor"] == 0 and held.counts["packets:sibling"] == 0
    # Once the Worker answers, the old packets stop counting.
    dispatch.answer()
    freed = runner.run([ep], _ctx(dispatch, max_outstanding=limits))
    assert freed.counts["outstanding:judge:anchor"] == 0
    assert freed.counts["packets:anchor"] == 1


# ---- review/53 PR5: adjudication, quote check, calibration sample, stability -----------------

ADJ = LANES["judge:adjudicator"]


def _adj_ctx(dispatch, tasks=("tag",), **extra):
    extra.setdefault("adjudicator", ADJ)
    extra.setdefault("calibration_sample_rate", 0.0)
    return _ctx(dispatch, tasks=tasks, **extra)


def _adjudicator_packets(dispatch):
    return [(p, j) for p, j in dispatch.submitted.values() if p.role == "adjudicator"]


def _judged_contested(ep, **extra):
    """Run a tag episode through anchor and sibling with the anchor saying yes, the sibling no."""
    dispatch = FakeDispatch()
    runner.run([ep], _adj_ctx(dispatch, **extra))
    dispatch.answer(anchor_p=0.9, verdicts={"sibling": False})
    return dispatch


def test_quotes_must_occur_verbatim_in_the_evidence():
    from citypods.judging.quotes import normalize, quote_valid

    evidence = "Council voted\n 5-2   to approve the Elm Street rezoning on Tuesday night"
    assert quote_valid('He said "voted 5-2 to approve the" it', evidence)
    # Typographic quotes, apostrophes and whitespace are normalised on both sides.
    assert quote_valid("\u201cto approve the Elm Street rezoning\u201d", evidence)
    assert quote_valid('"the council\'s rezoning vote"', "Tonight the council\u2019s rezoning vote")
    assert quote_valid('"Council voted ... Tuesday night"', evidence)
    # Not in the evidence, altered, or case-changed: invalid.
    assert not quote_valid('"voted 5-2 to reject the rezoning"', evidence)
    assert not quote_valid('"COUNCIL VOTED 5-2 to approve"', evidence)
    # No quote at all, a quote too short to prove anything, or a missing reason: invalid.
    assert not quote_valid("the evidence clearly supports it", evidence)
    assert not quote_valid('"rezoning"', evidence)
    assert not quote_valid(None, evidence)
    # One bad quote among good ones sinks the reason.
    assert not quote_valid('"voted 5-2 to approve" and "something never said here"', evidence)
    assert normalize("a b — c") == "a b - c"


def test_a_contested_subject_goes_to_an_independent_adjudicator_at_the_largest_tier():
    ep = _episode()
    dispatch = _judged_contested(ep)
    result = runner.run([ep], _adj_ctx(dispatch))
    packets = _adjudicator_packets(dispatch)
    assert result.counts["packets:adjudicator"] == len(packets) >= 1
    assert result.counts["adjudication_contested"] == 2  # the rule tag and the Gemini tag
    for packet, job in packets:
        assert packet.purpose == "judge:adjudicator" and packet.judge_model in ADJ.models
        assert {item.tier for item in packet.items} == {"T2"}
        assert {item.sample for item in packet.items} == {"contested"}
        assert job.inputs["messages"][0]["content"].startswith("You are the adjudicator")
        assert job.inputs["llm_policy"].allowed_models == (packet.judge_model,)
    # Independent of each entry's producer and sibling: the Gemini tag (Google) judged by the
    # NVIDIA sibling gets an adjudicator from neither family.
    families = {FAMILIES[p.judge_model] for p, _ in packets}
    assert not families & {"google", "nvidia"}


def test_an_adjudicators_answer_is_recorded_with_its_quote_check():
    ep = _episode()
    dispatch = _judged_contested(ep)
    runner.run([ep], _adj_ctx(dispatch))
    dispatch.answer(verdicts={"adjudicator": True})
    stats = runner.run([ep], _adj_ctx(dispatch))
    rows = [r for r in _tag_judgments(ep) if r["judge_role"] == "adjudicator"]
    assert len(rows) == 2 and stats.counts["quote_valid"] >= 2
    assert {r["quote_valid"] for r in rows} == {True}
    assert {r["sample"] for r in rows} == {"contested"} and {r["value"] for r in rows} == {True}
    assert {r["context_tier"] for r in rows} == {"T2"}
    # The sibling's answers were quote-checked too, and nothing is adjudicated twice.
    assert {r["quote_valid"] for r in _tag_judgments(ep) if r["judge_role"] == "sibling"} == {True}
    assert not _adjudicator_packets(dispatch)
    assert ep.uid in stats.episodes_complete


def test_an_answer_whose_quote_is_not_in_the_evidence_is_unsupported_and_ignored():
    ep = _episode()
    dispatch = _judged_contested(ep)
    runner.run([ep], _adj_ctx(dispatch))
    dispatch.answer(verdicts={"adjudicator": True}, bad_quotes=("adjudicator",))
    stats = runner.run([ep], _adj_ctx(dispatch))
    rows = [r for r in _tag_judgments(ep) if r["judge_role"] == "adjudicator"]
    assert stats.counts["quote_invalid"] == 2
    assert {r["quote_valid"] for r in rows} == {False}  # kept, never modified or deleted
    # An unsupported sibling answer no longer counts as a vote: the subject stops being contested
    # on it, so the evidence of disagreement is only what is supported.
    from citypods.judging import adjudication

    [subject] = [s for s in _subjects(ep) if s.payload.get("source_kind") == "rule"]
    assert adjudication._by_tier(subject, "supported", "adjudicator", None) == {}


def test_only_supported_votes_decide_whether_a_subject_is_contested():
    ep = _episode(llm=[])
    dispatch = FakeDispatch()
    runner.run([ep], _adj_ctx(dispatch))
    dispatch.answer(anchor_p=0.9, verdicts={"sibling": False}, bad_quotes=("sibling",))
    runner.run([ep], _adj_ctx(dispatch))
    # The dissenting sibling's quote was not in the evidence: its vote is unsupported, so the
    # subject is not contested and, with no calibration sample, nothing goes to the adjudicator.
    assert not _adjudicator_packets(dispatch)


def test_agreeing_judges_are_adjudicated_only_in_the_calibration_sample():
    ep = _episode()
    dispatch = FakeDispatch()
    runner.run([ep], _adj_ctx(dispatch, calibration_sample_rate=1.0))
    dispatch.answer(anchor_p=0.95, verdicts={"sibling": True})
    result = runner.run([ep], _adj_ctx(dispatch, calibration_sample_rate=1.0))
    assert result.counts["adjudication_calibration"] == 2
    assert result.counts["adjudication_contested"] == 0
    assert {i.sample for p, _ in _adjudicator_packets(dispatch) for i in p.items} == {"calibration"}
    # A contested item and a sampled one read the same to the adjudicator: it is never told.
    for _packet, job in _adjudicator_packets(dispatch):
        assert "contested" not in json.dumps(job.inputs["messages"]).lower()

    quiet = FakeDispatch()
    runner.run([_episode(uid="ep-q")], _adj_ctx(quiet))
    quiet.answer(anchor_p=0.95, verdicts={"sibling": True})
    none = runner.run([_episode(uid="ep-q")], _adj_ctx(quiet))
    assert not _adjudicator_packets(quiet) and none.counts["adjudication_calibration"] == 0


def test_the_calibration_sample_is_a_deterministic_share():
    from citypods.judging.adjudication import in_sample

    keys = [f"subject-{i}:supported:calibration" for i in range(4000)]
    chosen = [k for k in keys if in_sample(k, 0.03)]
    assert 70 <= len(chosen) <= 170  # about 3% of 4,000
    assert chosen == [k for k in keys if in_sample(k, 0.03)]
    assert not any(in_sample(k, 0.0) for k in keys) and all(in_sample(k, 1.0) for k in keys)


def test_a_probability_in_the_escalation_band_is_contested_even_when_the_judges_agree():
    ep = _episode(llm=[])
    dispatch = FakeDispatch()
    runner.run([ep], _adj_ctx(dispatch))
    dispatch.answer(anchor_p=0.55, verdicts={"sibling": True})  # both say yes; the anchor is unsure
    waiting = runner.run([ep], _adj_ctx(dispatch))  # the anchor escalates to T2 first
    assert waiting.counts["packets:anchor"] == 1 and not _adjudicator_packets(dispatch)
    assert waiting.counts["adjudication_contested"] == 0
    dispatch.answer(anchor_p=0.55, verdicts={"sibling": True})  # still unsure at T2
    result = runner.run([ep], _adj_ctx(dispatch))
    assert result.counts["adjudication_contested"] == 1

    settled = FakeDispatch()
    other = _episode(uid="ep-s", llm=[])
    runner.run([other], _adj_ctx(settled))
    settled.answer(anchor_p=0.55, verdicts={"sibling": True})
    runner.run([other], _adj_ctx(settled))
    settled.answer(anchor_p=0.95, verdicts={"sibling": True})  # T2 settles it above the band
    done = runner.run([other], _adj_ctx(settled))
    assert done.counts["adjudication_contested"] == 0 and not _adjudicator_packets(settled)


def test_grades_are_contested_only_beyond_one_level():
    from citypods.judging.adjudication import _contested

    spec = task("moment")
    grade = spec.question("usefulness")
    assert not _contested(spec, grade, {"T1": 2.0}, {"T1": 3})
    assert _contested(spec, grade, {"T1": 0.4}, {"T1": 3})
    gate = spec.question("publishable")
    assert _contested(spec, gate, {"T1": 0.9}, {"T1": False})
    assert not _contested(spec, gate, {"T1": 0.9}, {"T1": True})


def test_a_meetings_choose_question_is_contested_when_the_winner_flips_with_order():
    moments = [_moment("We will fix the bridge", 600), _moment("Taxes will not rise", 900)]
    ep = _episode(tags=[], llm=[], moments=moments)
    dispatch = FakeDispatch()
    runner.run([ep], _adj_ctx(dispatch, tasks=("moment",)))
    dispatch.answer()  # the anchor always answers "A": the winner flips when the order does
    result = runner.run([ep], _adj_ctx(dispatch, tasks=("moment",)))
    group = [
        item
        for p, _ in _adjudicator_packets(dispatch)
        for item in p.items
        if item.question.kind == "choose"
    ]
    assert result.counts["adjudication_contested"] >= 2
    assert sorted(i.question_id for i in group) == ["best", "best_r"]
    assert len({i.evidence.digest for i in group}) == 2  # both option orders
    assert all(len(i.subjects) == 2 and i.options for i in group)


def test_adjudication_is_off_without_an_adjudicator_lane():
    ep = _episode()
    dispatch = _judged_contested(ep, adjudicator=None)
    runner.run([ep], _ctx(dispatch))
    assert not _adjudicator_packets(dispatch)


def test_an_episode_is_dirty_while_its_adjudication_is_due_and_clean_after():
    ep = _episode()
    ep.transcript_key = "t.vtt"
    kwargs = {
        "anchor": LANES["judge:anchor"],
        "sibling": LANES["judge:sibling"],
        "families": FAMILIES,
        "all_tiers_sample_rate": 0.0,
        "calibration_sample_rate": 0.0,
    }
    dispatch = _judged_contested(ep)
    runner.run([ep], _ctx(dispatch))  # collect the answers; adjudication is not asked for yet
    assert not runner.episode_needs_judging(ep, [task("tag")], **kwargs)
    assert runner.episode_needs_judging(ep, [task("tag")], adjudicator=ADJ, **kwargs)
    runner.run([ep], _adj_ctx(dispatch))
    dispatch.answer(verdicts={"adjudicator": True})
    runner.run([ep], _adj_ctx(dispatch))
    assert not runner.episode_needs_judging(ep, [task("tag")], adjudicator=ADJ, **kwargs)


def test_an_answer_that_cannot_be_quote_checked_yet_waits_and_is_recorded_later():
    ep = _episode(llm=[])
    dispatch = FakeDispatch()
    runner.run([ep], _adj_ctx(dispatch))
    dispatch.answer()

    def down(_ep):
        raise runner.TextsUnavailable("storage outage")

    broken = _adj_ctx(dispatch)
    broken.texts_for = down
    waiting = runner.run([ep], broken)
    assert waiting.counts["quote_check_deferred"] >= 1
    assert not [r for r in _tag_judgments(ep) if r["judge_role"] == "sibling"]
    assert _any_pending(ep)  # the sibling's marker stays until its quote can be checked
    recovered = runner.run([ep], _adj_ctx(dispatch))
    assert [r for r in _tag_judgments(ep) if r["judge_role"] == "sibling"]
    assert recovered.counts["quote_valid"] >= 1


def test_the_quote_check_reads_a_bounded_number_of_transcripts_a_pass():
    eps = [_episode(uid=f"ep-{i}", llm=[]) for i in range(3)]
    dispatch = FakeDispatch()
    runner.run(eps, _adj_ctx(dispatch))
    dispatch.answer()
    stats = runner.run(eps, _adj_ctx(dispatch, quote_check_reads=1))
    assert stats.counts["quote_check_deferred"] >= 2
    finished = runner.run(eps, _adj_ctx(dispatch))
    assert (
        all([r for r in _tag_judgments(ep) if r["judge_role"] == "sibling"] for ep in eps)
        and finished.counts["quote_check_deferred"] == 0
    )


def test_a_sampled_subject_is_asked_again_worded_differently_and_flips_are_recorded():
    ep = _episode(llm=[])
    dispatch = FakeDispatch()
    ctx = lambda: _ctx(dispatch, stability_sample_rate=1.0)  # noqa: E731
    runner.run([ep], ctx())
    # Nothing to compare with until the first answers are in.
    assert not [i for p, _ in dispatch.submitted.values() for i in p.items if i.order == "_s"]
    dispatch.answer(anchor_p=0.9)
    runner.run([ep], ctx())
    probes = [i for p, _ in dispatch.submitted.values() for i in p.items if i.order == "_s"]
    assert probes and {i.sample for i in probes} == {"stability"}
    assert {i.question_id for i in probes} == {"supported_s"}
    assert all(i.text().startswith(i.evidence.text) for i in probes)  # evidence first, reworded
    dispatch.answer(anchor_p=0.1)  # the anchor changes its mind when asked again
    runner.run([ep], ctx())
    rows = _tag_judgments(ep)
    asked = [r for r in rows if r["question_id"] == "supported_s"]
    assert {r["judge_role"] for r in asked} == {"anchor", "sibling"}
    assert {r["sample"] for r in asked} == {"stability"}
    # Asked once only.
    final = runner.run([ep], ctx())
    assert final.counts["items_submitted"] == 0

    from citypods.judging.report import summarize

    record = {"published": "2026-10-01T00:00:00Z", "judging": ep.judging, "llm_tag_candidates": []}
    stability = summarize([record])["stability"]
    anchor = next(v for k, v in stability.items() if "jev" in k)
    assert anchor == {"pairs": 1, "flips": 1}


def test_the_judge_stage_enables_adjudication_only_by_config(monkeypatch):
    from citypods import stages

    seen = []

    def spy(episodes, judging):
        seen.append(judging)
        return runner.JudgingStats()

    monkeypatch.setattr(runner, "run", spy)
    monkeypatch.setattr(
        "citypods.compute.llm.dispatch_v2_learned_input_ratio", lambda *a, **k: None
    )
    for adjudication in (None, {"enabled": False}, {"enabled": True}):
        ctx = _stage_ctx(enabled=True)
        ctx.judging_config.update(
            {
                "calibration_sample_rate": 0.05,
                "stability_sample_rate": 0.04,
                "max_outstanding_packets": {"anchor": 1, "sibling": 2, "adjudicator": 3},
            }
        )
        if adjudication is not None:
            ctx.judging_config["adjudication"] = adjudication
        ep = _episode()
        ep.transcript_key, ep.transcript_format = "t.vtt", "vtt"
        stages.JudgeStage().process(None, SimpleNamespace(slug="x"), [ep], ctx)
        judging = seen[-1]
        on = bool(adjudication and adjudication["enabled"])
        assert (judging.adjudicator is not None) is on
        assert ("judge:adjudicator" in ctx.judge_run_caps) is on
        assert judging.calibration_sample_rate == 0.05 and judging.stability_sample_rate == 0.04
        assert judging.max_outstanding == {
            "judge:anchor": 1,
            "judge:sibling": 2,
            "judge:adjudicator": 3,
        }
    assert stages.JudgeStage.PURPOSES == ("judge:anchor", "judge:sibling", "judge:adjudicator")


def test_one_overlong_reason_does_not_discard_its_packets_other_answers():
    """CodeRabbit CLI review of PR5: the reply schema used to cap `reason` at the stored length, so
    one verbose adjudicator rejected the whole reply (up to 20 answers)."""
    from citypods.judging.backends import REASON_MAX

    ep = _episode(llm=[])
    dispatch = FakeDispatch()
    runner.run([ep], _adj_ctx(dispatch))
    # Answer the sibling packets with a 700-character reason on every item.
    for recipe, (packet, job) in list(dispatch.submitted.items()):
        reply = answer_job(job, sibling=True)
        if packet.role == "sibling":
            body = json.loads(reply["choices"][0]["message"]["content"])
            for answer in body["answers"]:
                quote = answer["reason"]
                answer["reason"] = f"{quote} " + "and so on " * 70
            reply = {"choices": [{"message": {"content": json.dumps(body)}}]}
        dispatch.records[recipe] = JobResult(task="judge", recipe_hash=recipe, output=reply)
    dispatch.submitted.clear()
    runner.run([ep], _adj_ctx(dispatch))
    rows = [r for r in _tag_judgments(ep) if r["judge_role"] == "sibling"]
    assert rows and all(len(r["reason"]) <= REASON_MAX for r in rows)
    assert {r["quote_valid"] for r in rows} == {True}  # checked against the full reason


# ---- review/53 PR6: the probe set --------------------------------------------------------------


def _probes(*verdicts):
    from citypods.judging.probes import Probe

    return [
        Probe(
            id=f"tag-{i}",
            task="tag",
            question_id="supported",
            tier="T1",
            evidence=(
                "Proposed tag: Zoning reform: a change to the zoning code\n"
                f"Transcript: council approved case number {i} to rezone the corridor tonight"
            ),
            verdict=verdict,
            source="audit:test",
        )
        for i, verdict in enumerate(verdicts)
    ]


def test_gold_lists_start_empty_and_validate_strictly():
    from citypods.judging.probes import GATING_ITEMS, gating, load_probes, parse_probes

    for name in ("tag", "moment"):
        assert load_probes(task(name)) == []
    spec = task("tag")
    good = {
        "id": "tag-1",
        "question_id": "supported",
        "tier": "T1",
        "evidence": "Proposed tag: x\nTranscript: some words here",
        "verdict": True,
    }
    assert parse_probes(spec, [good])[0].subject_id == "probe:tag-1"
    for bad in (
        {**good, "id": "Bad Id"},
        {**good, "question_id": "nope"},
        {**good, "tier": "T9"},
        {**good, "evidence": " "},
        {**good, "evidence": "x" * 13_000},
        {**good, "verdict": "yes"},
    ):
        with pytest.raises(ValueError):
            parse_probes(spec, [bad])
    with pytest.raises(ValueError):
        parse_probes(spec, [good, good])  # a repeated id
    with pytest.raises(ValueError, match="must start with moment-"):
        parse_probes(task("moment"), [{**good, "question_id": "supported"}])  # another task's id
    with pytest.raises(ValueError):
        parse_probes(task("moment"), [{**good, "question_id": "usefulness"}])  # not binary
    assert not gating(_probes(*[True] * (GATING_ITEMS - 1))) and gating(_probes(*[True] * 20))


def test_probes_ride_in_live_packets_and_their_answers_are_recorded_per_judge():
    ep = _episode(llm=[])
    dispatch = FakeDispatch()
    probes = _probes(True, False, True)
    ctx = lambda: _ctx(dispatch, probes=probes, probes_per_pass=2)  # noqa: E731
    first = runner.run([ep], ctx())
    asked = [i for p, _ in dispatch.submitted.values() for i in p.items if i.sample == "probe"]
    # Each judge with live work is asked two probes, inside the packets it already sends.
    assert first.counts["probes_asked:anchor"] == 2 and first.counts["probes_asked:sibling"] == 2
    assert len(asked) == 4
    assert first.counts["packets:anchor"] == 1 and first.counts["packets:sibling"] == 1
    assert all(i.subjects[0].subject_id.startswith("probe:") for i in asked)
    dispatch.answer(anchor_p=0.9, sibling=True)
    runner.run([ep], ctx())
    rows = [r for subject in ep.judging["subjects"].values() for r in subject.get("judgments", [])]
    probe_rows = [r for r in rows if r["subject_id"].startswith("probe:")]
    assert len(probe_rows) == 4 and {r["sample"] for r in probe_rows} == {"probe"}
    assert {r["quote_valid"] for r in probe_rows if r["judge_role"] == "sibling"} == {True}

    # Within one host a probe is asked of a judge once; across hosts the rotation reaches them all.
    again = runner.run([ep], ctx())
    assert not again.counts["probes_asked:anchor"]
    reached: set[str] = set()
    for n in range(8):
        host = _episode(uid=f"host-{n}", llm=[])
        flow = FakeDispatch()
        runner.run([host], _ctx(flow, probes=probes, probes_per_pass=2))
        reached |= {
            i.subjects[0].subject_id
            for p, _ in flow.submitted.values()
            if p.role == "anchor"
            for i in p.items
            if i.sample == "probe"
        }
    assert reached == {"probe:tag-0", "probe:tag-1", "probe:tag-2"}


def test_each_judge_meets_the_probes_in_its_own_order_and_only_when_it_has_live_work():
    from citypods.judging.probes import order_key

    probes = _probes(True, False, True, False, True, False)
    orders = {
        model: [p.id for p in sorted(probes, key=lambda p: order_key(p, f"{model}|subj-1"))]
        for model in ("m-anchor", "m-sibling")
    }
    assert orders["m-anchor"] != orders["m-sibling"]
    idle = FakeDispatch()
    quiet = runner.run([], _ctx(idle, probes=probes, probes_per_pass=2))
    assert not idle.submitted and not quiet.counts["probes_asked:anchor"]


def test_a_probe_in_flight_keeps_its_episode_dirty_and_does_not_look_superseded():
    ep = _episode(llm=[])
    ep.transcript_key = "t.vtt"
    dispatch = FakeDispatch()
    runner.run([ep], _ctx(dispatch, probes=_probes(True), probes_per_pass=1))
    dispatch.answer()
    runner.run([ep], _ctx(dispatch))  # collect the live answers; the probe answers came too
    # Re-open one probe marker by hand: the episode must be dirty on it alone.
    entry = next(e for k, e in ep.judging["subjects"].items() if k.startswith("probe:"))
    marker = dict(entry["judgments"][0])
    entry["pending"] = [{**marker, "recipe_hash": "gone", "qid": "probe:tag-0:supported:T1"}]
    kwargs = {
        "anchor": LANES["judge:anchor"],
        "sibling": LANES["judge:sibling"],
        "families": FAMILIES,
        "all_tiers_sample_rate": 0.0,
    }
    assert runner.episode_needs_judging(ep, [task("tag")], **kwargs)


def test_the_report_scores_probes_per_judge_and_keeps_them_out_of_live_statistics():
    from citypods.judging.report import summarize

    ep = _episode(llm=[])
    dispatch = FakeDispatch()
    probes = _probes(True, False)
    runner.run([ep], _ctx(dispatch, probes=probes, probes_per_pass=2))
    dispatch.answer(anchor_p=0.9, sibling=True)  # every judge says "supported" to everything
    runner.run([ep], _ctx(dispatch, probes=probes))
    record = {"published": "2026-10-01T00:00:00Z", "judging": ep.judging, "llm_tag_candidates": []}
    report = summarize([record], probes={p.id: p.verdict for p in probes})
    anchor = next(v for k, v in report["probes"].items() if "jev" in k)
    assert anchor == {"asked": 2, "scored": 2, "correct": 1, "unsupported": 0, "accuracy": 0.5}
    assert all(not k.startswith("probe") for k in report["agreement"])
    # Unsupported (bad-quote) sibling answers are counted, not scored.
    dispatch2 = FakeDispatch()
    ep2 = _episode(uid="ep-2", llm=[])
    runner.run([ep2], _ctx(dispatch2, probes=probes, probes_per_pass=2))
    dispatch2.answer(bad_quotes=("sibling",))
    runner.run([ep2], _ctx(dispatch2, probes=probes))
    record2 = {"published": "2026-10-01T00:00:00Z", "judging": ep2.judging}
    sibling = next(
        v
        for k, v in summarize([record2], probes={p.id: p.verdict for p in probes})["probes"].items()
        if "gemma" in k
    )
    assert sibling["unsupported"] == 2 and sibling["scored"] == 0 and sibling["accuracy"] is None


def test_the_stage_loads_probes_only_when_enabled(monkeypatch):
    from citypods import stages

    seen = []

    def spy(episodes, judging):
        seen.append(judging)
        return runner.JudgingStats()

    monkeypatch.setattr(runner, "run", spy)
    monkeypatch.setattr(
        "citypods.compute.llm.dispatch_v2_learned_input_ratio", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "citypods.judging.probes.load_probes", lambda spec, root=None: _probes(True)
    )
    for block in (None, {"enabled": False}, {"enabled": True, "per_pass": 3}):
        ctx = _stage_ctx(enabled=True)
        if block is not None:
            ctx.judging_config["probes"] = block
        ep = _episode()
        ep.transcript_key, ep.transcript_format = "t.vtt", "vtt"
        stages.JudgeStage().process(None, SimpleNamespace(slug="x"), [ep], ctx)
    assert [(bool(j.probes), j.probes_per_pass) for j in seen] == [
        (False, 0),
        (False, 0),
        (True, 3),
    ]
