"""Calibration report and threshold proposal per judged task (review/53 PR7).

``python -m citypods.judging.calibrate --task <task>`` reads the stored judgments, keeps the
subject-questions the adjudicator answered (every contested one, plus the random calibration
sample), weights them to the population, and fits per question kind:

- ``validate``: the pair ``(t_admit, t_reject)`` on the anchor's probability (admit needs the
  independent sibling to agree) that maximises weighted agreement with the adjudicator, subject to
  the upper 95% bound of the weighted false-accept rate being at most ``FALSE_ACCEPT_CAP``;
- ``gate``: the anchor flag level with the lowest false-hold rate at zero missed flags, and each
  sibling's miss and false-hold counts (safety errs to holding);
- ``grade``: agreement within one level; ``choose``: decided only when both orders agree.

Only the Gemini Decision 1 anchor is calibrated (maintainer decision 2026-11-21): JEV's
judgments are history and are never pooled with it (review/49 "What must be re-measured").
An item whose deciding sibling quote is not in the adjudicator's evidence window (the
2026-11-09 T2-window finding) is excluded and counted per question, as is one whose window could
not be rebuilt. With ``--write`` the proposal goes into ``config/site_config.yml``
``judging.tasks.<task>.thresholds`` with its policy fingerprint; the task stays in shadow mode, and
nothing reads the thresholds until graduation (PR8/PR9).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from citypods.judging import ledger
from citypods.judging.adjudication import CALIBRATION, CONTESTED, _tier_rank
from citypods.judging.families import family_of
from citypods.judging.tasks import TIERS, QuestionSpec, Subject, TaskSpec

ANCHOR_MODEL = "gemini/gemini-decision-1"
# The Gemini anchor took over on 2026-11-12; earlier rows are another anchor's history.
ANCHOR_SINCE = "2026-11-12"
# Maintainer decision 2026-11-21: a fixed cap on the upper 95% bound of the weighted false-accept
# rate, not "no worse than the retired gate" (which never admitted anything).
FALSE_ACCEPT_CAP = 0.05
Z95 = 1.959964
GRID = tuple(round(i / 100, 2) for i in range(1, 100))
SITE_CONFIG = Path("config/site_config.yml")
# A gate level that would hold more than this share of items the adjudicator cleared is not a
# threshold: the anchor does not separate flagged from clear, so none is proposed.
MAX_FALSE_HOLD = 0.5

# Why an adjudicated item was left out of the fit.
EXCLUDED_WINDOW = "sibling_quote_outside_adjudicator_window"
EXCLUDED_UNCHECKED = "adjudicator_window_unavailable"
EXCLUDED_INCOMPLETE = "missing_anchor_or_sibling"

# (subject, sibling row, adjudicator row) -> whether the sibling's quote is in the adjudicator's
# evidence; None when that evidence cannot be rebuilt.
WindowCheck = Callable[[Subject, Mapping[str, Any], Mapping[str, Any]], "bool | None | Deferred"]


@dataclass(frozen=True)
class Deferred:
    """A window check to run later, in bulk (transcript reads are slow one at a time)."""

    key: Any


@dataclass(frozen=True)
class Item:
    subject_id: str
    question_id: str
    sample: str
    weight: float
    anchor: Any
    sibling: Any
    adjudicator: Any
    sibling_model: str | None = None


@dataclass
class QuestionData:
    question: QuestionSpec
    items: list[Item] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    # Items whose window check was deferred: (item, check key), settled by ``settle_deferred``.
    deferred: list[tuple[Item, Any]] = field(default_factory=list)

    def exclude(self, reason: str) -> None:
        self.excluded[reason] = self.excluded.get(reason, 0) + 1


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _rows(
    subject: Subject,
    question_id: str,
    role: str,
    *,
    model: str | None = None,
    exclude_family: str | None = None,
    families: Mapping[str, str] | None = None,
    since: str | None = None,
) -> list[dict]:
    """Usable rows of one role, oldest tier first and newest last within a tier."""
    rows = [
        row
        for row in ledger.judgments(subject)
        if row.get("question_id") == question_id
        and row.get("judge_role") == role
        and (model is None or row.get("judge_model") == model)
        and not (
            exclude_family is not None
            and families is not None
            and family_of(str(row.get("judge_model") or ""), families) == exclude_family
        )
        and (since is None or str(row.get("judged_at") or "") >= since)
        and row.get("context_tier") in TIERS
        and ledger.usable_vote(row)
    ]
    return sorted(rows, key=lambda r: (_tier_rank(r.get("context_tier")), str(r.get("judged_at"))))


def _deciding(rows: Sequence[dict]) -> dict | None:
    """The row whose value counts: the newest at the highest tier."""
    return rows[-1] if rows else None


def collect(
    subjects_by_group: Iterable[Sequence[Subject]],
    spec: TaskSpec,
    *,
    calibration_rate: float,
    anchor_model: str = ANCHOR_MODEL,
    since: str | None = ANCHOR_SINCE,
    families: Mapping[str, str] | None = None,
    window_check: WindowCheck | None = None,
) -> dict[str, QuestionData]:
    """The weighted adjudicated items per question; ``subjects_by_group`` is one meeting each."""
    if not 0 < calibration_rate <= 1:
        raise ValueError("calibration_rate must be in (0, 1]")
    data = {q.id: QuestionData(q) for q in spec.questions}
    weights = {CONTESTED: 1.0, CALIBRATION: 1.0 / calibration_rate}
    excluded_family = family_of(anchor_model, families) if families is not None else None
    for subjects in subjects_by_group:
        for question in spec.questions:
            cell = data[question.id]
            if question.kind == "choose":
                _collect_choose(cell, subjects, anchor_model, since, weights)
                continue
            for subject in subjects:
                adjudicator = _deciding(_rows(subject, question.id, "adjudicator"))
                if adjudicator is None or adjudicator.get("sample") not in weights:
                    continue
                anchor = _deciding(
                    _rows(subject, question.id, "anchor", model=anchor_model, since=since)
                )
                sibling = _deciding(
                    _rows(
                        subject,
                        question.id,
                        "sibling",
                        exclude_family=excluded_family,
                        families=families,
                    )
                )
                if anchor is None or sibling is None:
                    cell.exclude(EXCLUDED_INCOMPLETE)
                    continue
                item = Item(
                    subject.subject_id,
                    question.id,
                    str(adjudicator["sample"]),
                    weights[adjudicator["sample"]],
                    anchor.get("value"),
                    sibling.get("value"),
                    adjudicator.get("value"),
                    sibling.get("judge_model"),
                )
                inside = True
                if (
                    window_check is not None
                    and sibling.get("quote_valid") is True
                    # The same evidence digest is the same window: the passing quote is in it.
                    and (
                        not sibling.get("evidence_digest")
                        or sibling.get("evidence_digest") != adjudicator.get("evidence_digest")
                    )
                ):
                    inside = window_check(subject, sibling, adjudicator)
                if isinstance(inside, Deferred):
                    cell.deferred.append((item, inside.key))
                elif inside is None:
                    cell.exclude(EXCLUDED_UNCHECKED)
                elif not inside:
                    cell.exclude(EXCLUDED_WINDOW)
                else:
                    cell.items.append(item)
    return data


def settle_deferred(data: Mapping[str, QuestionData], results: Mapping[Any, bool | None]) -> None:
    """Move each deferred item into the fit or the exclusion counts by its check result."""
    for cell in data.values():
        for item, key in cell.deferred:
            inside = results.get(key)
            if inside is None:
                cell.exclude(EXCLUDED_UNCHECKED)
            elif not inside:
                cell.exclude(EXCLUDED_WINDOW)
            else:
                cell.items.append(item)
        cell.deferred.clear()


def _collect_choose(cell, subjects, anchor_model, since, weights) -> None:
    if len(subjects) < 2:
        return
    key = subjects[0]  # the group's rows live on its first subject (runner._plan_choose)
    qid = cell.question.id
    forward = _deciding(_rows(key, qid, "adjudicator"))
    reverse = _deciding(_rows(key, f"{qid}_r", "adjudicator"))
    if forward is None or reverse is None or forward.get("sample") not in weights:
        return
    if forward.get("value") != reverse.get("value"):
        cell.exclude("adjudicator_order_unstable")
        return
    anchor_f = _deciding(_rows(key, qid, "anchor", model=anchor_model, since=since))
    anchor_r = _deciding(_rows(key, f"{qid}_r", "anchor", model=anchor_model, since=since))
    if anchor_f is None or anchor_r is None:
        cell.exclude(EXCLUDED_INCOMPLETE)
        return
    cell.items.append(
        Item(
            key.group or key.subject_id,
            qid,
            str(forward["sample"]),
            weights[forward["sample"]],
            (str(anchor_f.get("value")), str(anchor_r.get("value"))),
            None,
            str(forward.get("value")),
        )
    )


# Weighted statistics.


def wilson_upper(rate: float, n_eff: float, z: float = Z95) -> float:
    """Upper Wilson bound of a proportion; with no effective sample it is 1 (nothing known)."""
    if n_eff <= 0:
        return 1.0
    denominator = 1 + z * z / n_eff
    centre = rate + z * z / (2 * n_eff)
    spread = z * math.sqrt(rate * (1 - rate) / n_eff + z * z / (4 * n_eff * n_eff))
    return min(1.0, (centre + spread) / denominator)


def kish(weights: Sequence[float]) -> float:
    """Kish's effective sample size of a weighted set."""
    total = sum(weights)
    squares = sum(w * w for w in weights)
    return total * total / squares if squares else 0.0


def _verdict(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    number = _number(value)
    return None if number is None else number >= 0.5


def validate_decision(item: Item, t_admit: float, t_reject: float) -> bool | None:
    p = _number(item.anchor)
    if p is None:
        return None
    if p >= t_admit and _verdict(item.sibling) is True:
        return True
    if p <= t_reject:
        return False
    return None  # undecided: the adjudicator decides


def _aggregate(items: Sequence[Item]) -> list[tuple]:
    """Weighted counts per distinct (anchor p, sibling verdict, adjudicator verdict).

    The anchor's answers take few distinct values, so the threshold search scores these cells
    instead of every item: ``(p, sibling_true, truth, weight, weight_squared, count)``."""
    cells: dict[tuple, list[float]] = {}
    for item in items:
        p, truth = _number(item.anchor), _verdict(item.adjudicator)
        if p is None or truth is None:
            continue
        cell = cells.setdefault((p, _verdict(item.sibling) is True, truth), [0.0, 0.0, 0])
        cell[0] += item.weight
        cell[1] += item.weight * item.weight
        cell[2] += 1
    return [(*key, *value) for key, value in cells.items()]


def _score(cells: Sequence[tuple], t_admit: float, t_reject: float) -> dict:
    total = decided = agree = negative_weight = false_accepts = 0.0
    negative_squares = 0.0
    negatives = 0
    for p, sibling_true, truth, weight, squares, count in cells:
        total += weight
        decision = True if (p >= t_admit and sibling_true) else (False if p <= t_reject else None)
        if decision is not None:
            decided += weight
            agree += weight * (decision == truth)
        if not truth:
            negative_weight += weight
            negative_squares += squares
            negatives += count
            if decision is True:
                false_accepts += weight
    rate = false_accepts / negative_weight if negative_weight else 0.0
    n_eff = negative_weight * negative_weight / negative_squares if negative_squares else 0.0
    return {
        "t_admit": t_admit,
        "t_reject": t_reject,
        "agreement": round(agree / total, 4) if total else None,
        "decided_share": round(decided / total, 4) if total else None,
        "false_accept_rate": round(rate, 4) if negative_weight else None,
        "false_accept_upper95": round(wilson_upper(rate, n_eff), 4),
        "adjudicated_negatives": negatives,
    }


def score_validate(items: Sequence[Item], t_admit: float, t_reject: float) -> dict:
    return _score(_aggregate(items), t_admit, t_reject)


def fit_validate(items: Sequence[Item], cap: float = FALSE_ACCEPT_CAP) -> dict:
    """The best pair under the cap; ties prefer the stricter admit and the looser reject."""
    cells = _aggregate(items)
    best = None
    for t_admit in GRID:
        for t_reject in GRID:
            if t_reject >= t_admit:
                break
            scored = _score(cells, t_admit, t_reject)
            if scored["agreement"] is None or scored["false_accept_upper95"] > cap:
                continue
            key = (scored["agreement"], t_admit, -t_reject)
            if best is None or key > best[0]:
                best = (key, scored)
    if best is None:
        return {"proposal": None, "reason": f"no pair keeps the false-accept upper bound <= {cap}"}
    return {"proposal": best[1]}


def fit_gate(items: Sequence[Item]) -> dict:
    """The anchor's highest flag level that misses no adjudicator flag, and its false holds."""
    labelled = [(i, _verdict(i.adjudicator)) for i in items]
    flagged = [i for i, truth in labelled if truth is True]
    clear = [i for i, truth in labelled if truth is False]
    probabilities = [_number(i.anchor) for i in flagged]
    if any(p is None for p in probabilities):
        return {"proposal": None, "reason": "an adjudicator flag has no anchor probability"}
    flag_at = min(probabilities, default=None)
    clear_weight = sum(i.weight for i in clear)
    held = sum(
        i.weight for i in clear if flag_at is not None and (_number(i.anchor) or 0.0) >= flag_at
    )
    siblings: dict[str, dict[str, float]] = {}
    for item, truth in labelled:
        vote = _verdict(item.sibling)
        if vote is None or truth is None:
            continue
        cell = siblings.setdefault(str(item.sibling_model), {"missed": 0, "false_holds": 0.0})
        cell["missed"] += truth and not vote
        cell["false_holds"] += item.weight if (vote and not truth) else 0.0
    return {
        "proposal": None
        if flag_at is None
        else {
            # The level is floored to the grid so a reading equal to the lowest flag still holds.
            "flag_at": math.floor(flag_at * 100) / 100,
            "false_hold_rate": round(held / clear_weight, 4) if clear_weight else None,
            "adjudicator_flags": len(flagged),
        },
        **({"reason": "the adjudicator flagged nothing"} if flag_at is None else {}),
        "siblings": {
            model: {"missed": int(c["missed"]), "false_hold_weight": round(c["false_holds"], 2)}
            for model, c in sorted(siblings.items())
        },
    }


def fit_grade(items: Sequence[Item]) -> dict:
    total = agree = 0.0
    for item in items:
        a, b = _number(item.anchor), _number(item.adjudicator)
        if a is None or b is None:
            continue
        total += item.weight
        agree += item.weight * (abs(a - b) <= 1)
    return {"within_one_level": round(agree / total, 4) if total else None}


def fit_choose(items: Sequence[Item]) -> dict:
    total = decided = agree = 0.0
    for item in items:
        forward, reverse = item.anchor
        total += item.weight
        if forward == reverse:
            decided += item.weight
            agree += item.weight * (forward == item.adjudicator)
    return {
        "decided_share": round(decided / total, 4) if total else None,
        "agreement_when_decided": round(agree / decided, 4) if decided else None,
    }


FITS = {"validate": fit_validate, "gate": fit_gate, "grade": fit_grade, "choose": fit_choose}


def report(
    data: Mapping[str, QuestionData],
    spec: TaskSpec,
    *,
    anchor_model: str = ANCHOR_MODEL,
    since: str | None = ANCHOR_SINCE,
    calibration_rate: float,
    cap: float = FALSE_ACCEPT_CAP,
) -> dict:
    questions = {}
    for question_id, cell in data.items():
        fit = (
            fit_validate(cell.items, cap)
            if cell.question.kind == "validate"
            else FITS[cell.question.kind](cell.items)
        )
        samples = {CONTESTED: 0, CALIBRATION: 0}
        for item in cell.items:
            samples[item.sample] = samples.get(item.sample, 0) + 1
        questions[question_id] = {
            "kind": cell.question.kind,
            "prompt_version": cell.question.prompt_version,
            "items": samples,
            "excluded": dict(sorted(cell.excluded.items())),
            **fit,
        }
    policy = {
        "task": spec.name,
        "anchor_model": anchor_model,
        "anchor_since": since,
        "false_accept_cap": cap,
        "calibration_sample_rate": calibration_rate,
        "questions": {q: (v["kind"], v["prompt_version"]) for q, v in questions.items()},
    }
    return {
        "task": spec.name,
        "policy": policy,
        "policy_fingerprint": fingerprint(policy),
        "questions": questions,
    }


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def thresholds(result: Mapping[str, Any]) -> dict[str, Any]:
    """The config block: only proposed values, plus the fingerprint they were fitted under."""
    out: dict[str, Any] = {
        "policy_fingerprint": result["policy_fingerprint"],
        "anchor_model": result["policy"]["anchor_model"],
        "false_accept_cap": result["policy"]["false_accept_cap"],
    }
    for question_id, cell in result["questions"].items():
        proposal = cell.get("proposal")
        if not proposal:
            continue
        if cell["kind"] == "validate":
            out[question_id] = {"t_admit": proposal["t_admit"], "t_reject": proposal["t_reject"]}
        elif cell["kind"] == "gate" and (proposal["false_hold_rate"] or 0.0) <= MAX_FALSE_HOLD:
            out[question_id] = {"flag_at": proposal["flag_at"]}
    return out


def write_thresholds(text: str, task_name: str, block: Mapping[str, Any]) -> str:
    """Set ``judging.tasks.<task>.thresholds`` in the site config text, keeping its comments.

    The task line is a one-line flow mapping (``    tag: {mode: shadow}``); its other keys keep
    their values and order."""
    import yaml

    pattern = re.compile(rf"^(    {re.escape(task_name)}: )(\{{.*\}})[ \t]*$", re.M)
    matches = list(pattern.finditer(text))
    judging_at = text.find("\njudging:\n")
    tasks_at = text.find("\n  tasks:\n", judging_at)
    matches = [m for m in matches if judging_at >= 0 and tasks_at >= 0 and m.start() > tasks_at]
    if not matches:
        raise ValueError(f"judging.tasks.{task_name} is not a one-line mapping in the site config")
    match = matches[0]
    current = yaml.safe_load(match.group(2))
    if not isinstance(current, dict):
        raise ValueError(f"judging.tasks.{task_name} is not a mapping")
    current = {k: v for k, v in current.items() if k != "thresholds"}
    current["thresholds"] = dict(block)
    flow = yaml.safe_dump(current, default_flow_style=True, sort_keys=False, width=10_000).strip()
    updated = text[: match.start()] + match.group(1) + flow + text[match.end() :]
    loaded = yaml.safe_load(updated)
    if loaded["judging"]["tasks"][task_name] != current:
        raise ValueError("the edited site config does not read back as written")
    return updated


# Production wiring: stored episodes and their transcripts.


class WindowChecks:
    """Defers each window check during ``collect`` and runs them afterwards, reading each needed
    transcript once and several at a time.

    The rebuilt evidence must match the adjudicator row's stored digest; anything else (no
    transcript, a changed transcript, a tier the builder cannot produce) is ``None``."""

    def __init__(self, storage, definitions, spec: TaskSpec, *, workers: int = 16, log=None):
        self.storage = storage
        self.definitions = definitions
        self.spec = spec
        self.workers = workers
        self.log = log or (lambda message: None)
        self.pending: dict[str, list[tuple[Any, Subject, dict, dict]]] = {}
        self.episodes: dict[str, Any] = {}

    def __call__(self, subject, sibling_row, adjudicator_row):
        episode = _EPISODES.get(subject.episode_uid)
        if episode is None or not getattr(episode, "transcript_key", None):
            return None
        key = (subject.subject_id, sibling_row.get("question_id"))
        self.episodes[subject.episode_uid] = episode
        self.pending.setdefault(subject.episode_uid, []).append(
            (key, subject, dict(sibling_row), dict(adjudicator_row))
        )
        return Deferred(key)

    def _episode(self, uid: str) -> dict[Any, bool | None]:
        from citypods.judging.quotes import quote_valid
        from citypods.judging.texts import build_episode_texts
        from citypods.stages import _read_storage_bytes

        episode = self.episodes[uid]
        out: dict[Any, bool | None] = {}
        try:
            raw = _read_storage_bytes(self.storage, episode.transcript_key)
            texts = build_episode_texts(episode, raw, self.definitions)
        except Exception as exc:  # noqa: BLE001 - one unreadable transcript excludes its items
            self.log(f"transcript {uid}: {type(exc).__name__}: {exc}")
            return {key: None for key, *_ in self.pending[uid]}
        for key, subject, sibling_row, adjudicator_row in self.pending[uid]:
            try:
                evidence = self.spec.evidence(subject, adjudicator_row.get("context_tier"), texts)
            except (KeyError, ValueError, TypeError):
                evidence = None
            if evidence is None or evidence.digest != adjudicator_row.get("evidence_digest"):
                out[key] = None
            else:
                out[key] = quote_valid(sibling_row.get("reason"), evidence.text)
        return out

    def run(self, cache: Path | None = None) -> dict[Any, bool | None]:
        """Run the deferred checks; with ``cache``, reuse and save results as a JSON file."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        results: dict[Any, bool | None] = {}
        saved: dict[str, bool | None] = {}
        if cache is not None and cache.exists():
            saved = json.loads(cache.read_text())
            for uid in list(self.pending):
                keys = [f"{k[0]}\x1f{k[1]}" for k, *_ in self.pending[uid]]
                if all(k in saved for k in keys):
                    for (key, *_), name in zip(self.pending.pop(uid), keys, strict=True):
                        results[key] = saved[name]
        total = len(self.pending)
        self.log(f"window checks: {sum(map(len, self.pending.values()))} items in {total} episodes")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._episode, uid) for uid in self.pending]
            for done, future in enumerate(as_completed(futures), 1):
                results.update(future.result())
                if done % 100 == 0 or done == total:
                    self.log(f"window checks: {done}/{total} episodes read")
        if cache is not None:
            saved.update({f"{k[0]}\x1f{k[1]}": v for k, v in results.items()})
            cache.write_text(json.dumps(saved))
        return results


_EPISODES: dict[str, Any] = {}


def _groups(site, state_dir: Path, config_dir: str, spec: TaskSpec):
    from citypods.config import load_city_configs
    from citypods.records import load_records, record_to_episode, source_key

    for city in load_city_configs(config_dir, site.get("defaults", {})):
        for raw in load_records(state_dir, source_key(city)).values():
            episode = record_to_episode(raw)
            subjects = spec.subjects(episode)
            if not subjects:
                continue
            _EPISODES.clear()
            _EPISODES[subjects[0].episode_uid] = episode
            if spec.selection_policy == "top_k_per_group":
                groups: dict[Any, list[Subject]] = {}
                for subject in subjects:
                    groups.setdefault(subject.group, []).append(subject)
                yield from groups.values()
            else:
                yield subjects


def markdown(result: Mapping[str, Any]) -> str:
    """The evidence table for the PR description."""
    lines = [
        f"### `{result['task']}` (policy `{result['policy_fingerprint']}`)",
        "",
        "| Question | Kind | Contested | Calibration | Excluded | Result |",
        "|---|---|---|---|---|---|",
    ]
    for question_id, cell in result["questions"].items():
        excluded = ", ".join(f"{k}: {v}" for k, v in cell["excluded"].items()) or "none"
        shown = {
            k: v
            for k, v in cell.items()
            if k not in {"kind", "prompt_version", "items", "excluded"}
        }
        lines.append(
            f"| `{question_id}` | {cell['kind']} | {cell['items'].get(CONTESTED, 0)} | "
            f"{cell['items'].get(CALIBRATION, 0)} | {excluded} | `{json.dumps(shown)}` |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--site-config", default=str(SITE_CONFIG))
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output-dir", default="public")
    parser.add_argument("--anchor-model", default=ANCHOR_MODEL)
    parser.add_argument("--since", default=ANCHOR_SINCE, help="first anchor judgment date counted")
    parser.add_argument("--false-accept-cap", type=float, default=FALSE_ACCEPT_CAP)
    parser.add_argument("--window-cache", type=Path, help="JSON file of window-check results")
    parser.add_argument("--no-pull", action="store_true", help="use the local state as it is")
    parser.add_argument("--write", action="store_true", help="write thresholds into site config")
    parser.add_argument("--markdown", type=Path, help="also write the PR evidence table here")
    args = parser.parse_args(argv)

    from citypods.compute.llm_lanes import load_families
    from citypods.config import load_site_config
    from citypods.judging.tasks import task
    from citypods.judging.texts import tag_definitions
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state
    from citypods.storage import make_storage

    site = load_site_config(args.site_config)
    spec = task(args.task)
    state_dir = resolve_state_dir(site, Path(args.output_dir))
    storage = make_storage(site, site.get("base_url", ""), Path(args.output_dir))

    def log(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    if not args.no_pull:
        # Only the episode records hold judgments; the log goes to stderr, not the JSON on stdout.
        from citypods.config import load_city_configs
        from citypods.records import source_key

        cities = load_city_configs(args.config_dir, site.get("defaults", {}))
        pull_state(
            storage,
            state_dir,
            only_paths=sorted({f"sources/{source_key(c)}/episodes.json" for c in cities}),
            apply_tombstones=False,
            log=log,
        )
    rate = float((site.get("judging") or {}).get("calibration_sample_rate", 0.03))
    checks = WindowChecks(storage, tag_definitions(), spec, log=log)
    data = collect(
        _groups(site, state_dir, args.config_dir, spec),
        spec,
        calibration_rate=rate,
        anchor_model=args.anchor_model,
        since=args.since,
        families=load_families(),
        window_check=checks,
    )
    settle_deferred(data, checks.run(args.window_cache))
    result = report(
        data,
        spec,
        anchor_model=args.anchor_model,
        since=args.since,
        calibration_rate=rate,
        cap=args.false_accept_cap,
    )
    print(json.dumps(result, indent=2))
    if args.markdown:
        args.markdown.write_text(markdown(result))
    if args.write:
        path = Path(args.site_config)
        path.write_text(write_thresholds(path.read_text(), spec.name, thresholds(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Item",
    "QuestionData",
    "collect",
    "fit_choose",
    "fit_gate",
    "fit_grade",
    "fit_validate",
    "report",
    "thresholds",
    "write_thresholds",
]
