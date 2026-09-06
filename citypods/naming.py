"""R7 naming gate: signal fusion and the adaptive admission policy (review/31 §C.4).

`citypods.speakers` owns the signal *producers* (chair-recognition cues, self-introduction cues,
voice-profile matching, roster ingestion). This module owns the *policy* over them: which
proposals agree, whether that agreement is enough to name someone, and which signal combinations
have earned the right to be trusted without a human.

It replaces the flat "30 reviews x 30 days x 95% precision per (city, body, engine, capture
context)" gate, which multiplied as `30 x cities x bodies` -- a threshold no detection improvement
can scale past. The model here instead *learns* which combinations are reliable from the human
verdicts already being collected, so automation removes review items rather than reordering them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from citypods.speakers import TIER_MEMBER, TIER_OTHER, TIER_STAFF, _norm

# --- Signals -----------------------------------------------------------------------------------

# **Timed** signals: each one observes something at a specific point in the recording, so it can
# say *this cluster, here, is N*. At least one is always required -- without it there is no
# proposal about a voice at all, only a fact about the meeting.
SIGNAL_VOICE_PRINT = "voice-print"
SIGNAL_CHAIR_CUE = "chair-reference"
SIGNAL_SELF_INTRO = "self-introduction"
# **Untimed** signals: true of the meeting or the body, never of a moment in the audio. Roster
# says "this name was in the room per the minutes"; membership says "this person sits on this
# body" (carried forward from prior meetings' rosters while this meeting's minutes are still
# unpublished -- often weeks). Both make a proposal plausible; neither can locate it in time.
SIGNAL_ROSTER = "roster"
SIGNAL_MEMBERSHIP = "membership"
# A title spoken alongside a name ("Matt Bodine, Assistant Planner"). Timed, but not originating:
# it arrives in the same utterance as the name it qualifies, so it corroborates rather than
# independently proposing. Counts toward agreement for the staff tier only -- see
# `meets_agreement_rule`.
SIGNAL_TITLE_CUE = "title-cue"

ORIGINATING_SIGNALS = frozenset({SIGNAL_VOICE_PRINT, SIGNAL_CHAIR_CUE, SIGNAL_SELF_INTRO})
# The invariant this set exists to make unmissable: **no combination drawn only from here may
# ever name anyone**, however many of them agree. `{roster, membership}` is two signals and still
# means only "a person by this name plausibly belongs at this meeting" -- it contains nothing that
# ties the name to the voice being labelled. `meets_agreement_rule`'s originating requirement
# already enforces this, but it did so as a side effect of a separate rule; naming the set keeps a
# later "just let two corroborating signals through" from quietly removing the protection.
UNTIMED_SIGNALS = frozenset({SIGNAL_ROSTER, SIGNAL_MEMBERSHIP})
_BASE_SIGNALS = ORIGINATING_SIGNALS | UNTIMED_SIGNALS

# Defaults for the adaptive gate (review/31 §C.4.4). Config, not constants: the first real verdict
# data may well argue for moving them. There is deliberately no calendar element -- the old gate's
# 30-day requirement existed to catch capture drift, and the per-city divergence guardrail
# (`city_diverges`) does that job directly and faster.
DEFAULT_MIN_VERDICTS = 20
DEFAULT_MIN_PRECISION = 0.95
# How far a single city's observed agreement may fall below the global prior before that city
# falls back to human review. Pooling globally is what lets city #2 start mostly automated; this
# is the check that stops a city with genuinely worse audio inheriting optimism it hasn't earned.
DEFAULT_CITY_DIVERGENCE_MARGIN = 0.10
DEFAULT_CITY_MIN_VERDICTS = 10

# Correct human rulings on one *person* before that person's name stops needing a human every
# time (review/31 §C.4.12). Much smaller than `DEFAULT_MIN_VERDICTS` because it answers a much
# narrower question -- "is this the right name, spelled the right way, for someone on this body"
# -- and because the same rulings simultaneously feed the combination statistic, so the two
# thresholds are paid for by one queue rather than two.
DEFAULT_MIN_MEMBER_VERDICTS = 4


@dataclass(frozen=True)
class NameProposal:
    """One signal's claim that `cluster` is `display_name`."""

    cluster: str
    display_name: str
    signal: str


@dataclass(frozen=True)
class FusedCandidate:
    """Every signal that independently agreed on one (cluster, name) pair."""

    cluster: str
    display_name: str
    tier: str
    signals: tuple[str, ...]

    @property
    def combination_key(self) -> str:
        """Key for the precision table: tier plus the sorted signal set.

        Tier is part of the key because the same combination genuinely differs in reliability
        between tiers -- roster corroboration is strong for a member (they are on the roster by
        definition) and weak for staff (who often are not).
        """
        return f"{self.tier}:{'+'.join(self.signals)}"


def _countable_signals(signals: Iterable[str], tier: str) -> set[str]:
    """Signals that count toward agreement for this tier.

    The staff exception (review/31 §C.4.4): staff frequently appear on no parseable roster and
    have no voice print in a new city, leaving only their own self-introduction. Under a flat
    two-signal rule that would silently turn "staff are auto-named" into "staff are never named",
    so a title cue counts as the second signal *for staff only*. The evidence is knowingly
    correlated -- name and title come from one utterance and can be wrong together -- which is
    acceptable because staff are the unverified-by-policy tier and because this combination is
    tracked under its own `combination_key`: if it proves unreliable the gate observes that
    directly and stops trusting it, rather than the risk being assumed away.
    """
    allowed = set(_BASE_SIGNALS)
    if tier == TIER_STAFF:
        allowed.add(SIGNAL_TITLE_CUE)
    return {signal for signal in signals if signal in allowed}


def meets_agreement_rule(signals: Iterable[str], tier: str) -> bool:
    """At least two countable signals agree, at least one of them timed (originating).

    The second condition is not a tie-breaker, it is the substantive one: `UNTIMED_SIGNALS` say
    who plausibly belongs at this meeting, and no number of them agreeing says anything about
    *which voice* is being labelled.
    """
    countable = _countable_signals(signals, tier)
    if len(countable) < 2:
        return False
    return bool(countable & ORIGINATING_SIGNALS)


def fuse_proposals(
    proposals: Iterable[NameProposal],
    *,
    tier_of: Any,
) -> list[FusedCandidate]:
    """Collapse per-signal proposals into one candidate per (cluster, name).

    A reviewer should see one best-supported suggestion carrying which signals agreed, never
    several half-signals each demanding their own look (review/31 §C.3). `tier_of` maps a display
    name to its tier.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for proposal in proposals:
        name = str(proposal.display_name or "").strip()
        if not name or not proposal.cluster:
            continue
        key = (str(proposal.cluster), _norm(name))
        row = grouped.setdefault(key, {"display_name": name, "signals": set()})
        row["signals"].add(proposal.signal)
    candidates: list[FusedCandidate] = []
    for (cluster, _), row in grouped.items():
        candidates.append(
            FusedCandidate(
                cluster=cluster,
                display_name=row["display_name"],
                tier=tier_of(row["display_name"]),
                signals=tuple(sorted(row["signals"])),
            )
        )
    return candidates


# --- Precision table ---------------------------------------------------------------------------


class PrecisionTable:
    """Agreement between automatic candidates and human verdicts, per signal combination.

    Pooled **globally** across cities and bodies, which is the whole point: it is what lets a new
    city start mostly automated instead of re-earning trust from zero. Per-city counts are kept
    alongside purely as the divergence guardrail, not as a second gate.

    Deliberately *derived*, never persisted: it is rebuilt from the append-only review ledger on
    every run (see `from_evaluation`). A separate saved copy would be a second source of truth
    that could silently drift from the verdicts it claims to summarize, and rebuilding costs one
    pass over a ledger measured in thousands of rows.
    """

    def __init__(self) -> None:
        self._global: dict[str, dict[str, int]] = {}
        self._by_city: dict[str, dict[str, dict[str, int]]] = {}
        # Per *person*, scoped to one body: "have humans agreed this is who this is, spelled this
        # way". Never pooled across cities the way combination statistics are -- a person belongs
        # to one body, so there is nothing for a second city to inherit.
        self._by_person: dict[str, dict[str, int]] = {}

    @classmethod
    def from_evaluation(cls, evaluation: Mapping[str, Any]) -> PrecisionTable:
        """Rebuild the table by joining human verdicts to the candidates they ruled on.

        A verdict row records only *which candidate* was judged, so the signal combination comes
        from the candidate ledger. Rows whose candidate is unknown -- verdicts recorded against
        pre-naming-gate candidates, or a ledger trimmed at some point -- are skipped rather than
        guessed at: an unattributable verdict must not inflate any combination's precision.
        """
        table = cls()
        candidates = evaluation.get("naming_candidates")
        if not isinstance(candidates, Mapping):
            return table
        for row in evaluation.get("reviews") or ():
            if not isinstance(row, Mapping):
                continue
            candidate = candidates.get(str(row.get("candidate_id") or ""))
            if not isinstance(candidate, Mapping):
                continue
            key = str(candidate.get("combination_key") or "")
            city_slug = str(candidate.get("city_slug") or "")
            if not (key and city_slug):
                continue
            agreed = bool(row.get("correct"))
            table.record(key, city_slug=city_slug, agreed=agreed)
            if str(candidate.get("tier") or "") == TIER_MEMBER:
                table.record_person(
                    str(candidate.get("display_name") or ""),
                    city_slug=city_slug,
                    body=str(candidate.get("body") or ""),
                    agreed=agreed,
                )
        return table

    @staticmethod
    def person_key(display_name: str, *, city_slug: str, body: str) -> str:
        return f"{city_slug}|{_norm(body)}|{_norm(display_name)}"

    def record_person(self, display_name: str, *, city_slug: str, body: str, agreed: bool) -> None:
        """Fold one human verdict about a specific person into the table."""
        name = _norm(display_name)
        if not name:
            return
        bucket = self._by_person.setdefault(
            self.person_key(display_name, city_slug=city_slug, body=body),
            {"verdicts": 0, "agreements": 0},
        )
        bucket["verdicts"] += 1
        bucket["agreements"] += 1 if agreed else 0

    def person_established(
        self,
        display_name: str,
        *,
        city_slug: str,
        body: str,
        min_verdicts: int = DEFAULT_MIN_MEMBER_VERDICTS,
        min_precision: float = DEFAULT_MIN_PRECISION,
    ) -> bool:
        """Whether humans have settled this person's identity and spelling for this body.

        Requires precision as well as a count, so a person whose rulings later go bad loses the
        status rather than keeping it on the strength of early agreement. At the default four
        verdicts that means four correct; a disagreement is recoverable by accumulating more.
        """
        row = self._by_person.get(self.person_key(display_name, city_slug=city_slug, body=body))
        if not row or row["verdicts"] < min_verdicts:
            return False
        return row["agreements"] / row["verdicts"] >= min_precision

    def record(self, combination_key: str, *, city_slug: str, agreed: bool) -> None:
        """Fold one human verdict into the table."""
        for bucket in (
            self._global.setdefault(combination_key, {"verdicts": 0, "agreements": 0}),
            self._by_city.setdefault(city_slug, {}).setdefault(
                combination_key, {"verdicts": 0, "agreements": 0}
            ),
        ):
            bucket["verdicts"] += 1
            bucket["agreements"] += 1 if agreed else 0

    def precision(self, combination_key: str, *, city_slug: str | None = None) -> float | None:
        rows = self._by_city.get(city_slug or "", {}) if city_slug else self._global
        row = rows.get(combination_key)
        if not row or row["verdicts"] <= 0:
            return None
        return row["agreements"] / row["verdicts"]

    def verdicts(self, combination_key: str, *, city_slug: str | None = None) -> int:
        rows = self._by_city.get(city_slug or "", {}) if city_slug else self._global
        return rows.get(combination_key, {}).get("verdicts", 0)

    def trusted(
        self,
        combination_key: str,
        *,
        min_verdicts: int = DEFAULT_MIN_VERDICTS,
        min_precision: float = DEFAULT_MIN_PRECISION,
    ) -> bool:
        """Whether this combination has earned auto-admission. Fail-closed before evidence."""
        if self.verdicts(combination_key) < min_verdicts:
            return False
        precision = self.precision(combination_key)
        return precision is not None and precision >= min_precision

    def city_diverges(
        self,
        combination_key: str,
        *,
        city_slug: str,
        margin: float = DEFAULT_CITY_DIVERGENCE_MARGIN,
        min_verdicts: int = DEFAULT_CITY_MIN_VERDICTS,
    ) -> bool:
        """Whether this city under-performs the global prior enough to fall back to review.

        Requires a minimum local sample first: two unlucky verdicts in a new city should not
        revoke trust that a thousand verdicts elsewhere established.
        """
        if self.verdicts(combination_key, city_slug=city_slug) < min_verdicts:
            return False
        local = self.precision(combination_key, city_slug=city_slug)
        overall = self.precision(combination_key)
        if local is None or overall is None:
            return False
        return local < overall - margin


# --- The gate ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class NamingDecision:
    """What to do with one fused candidate."""

    candidate: FusedCandidate
    publish: bool
    needs_review: bool
    reason: str


def decide(
    candidate: FusedCandidate,
    table: PrecisionTable,
    *,
    city_slug: str,
    body: str = "",
    min_verdicts: int = DEFAULT_MIN_VERDICTS,
    min_precision: float = DEFAULT_MIN_PRECISION,
    min_member_verdicts: int = DEFAULT_MIN_MEMBER_VERDICTS,
    confirmed_names: Iterable[str] = (),
) -> NamingDecision:
    """Apply the tiered naming policy to one candidate (review/31 §C.4).

    Members need **two** independent things before their name publishes unattended, because two
    different questions are being asked and one answer does not cover the other:

    * *Who is this person, spelled how?* — settled per person, by a small number of human rulings
      (`person_established`) or by an approved voice profile. This is what a reviewer is actually
      good at, and it is where an ASR misspelling gets corrected.
    * *Is this cluster that person, here, in this meeting?* — carried entirely by the signals that
      agreed, which is what the precision table measures. Confirming a member four times says
      nothing about whether a *fifth* meeting's cluster is really them, so dropping this check
      would let a well-known name be attached on a combination never shown to be reliable. For a
      tier that earns a cross-meeting speaker page, that misattribution is worse than silence.

    Staff need only the second, having no page and no per-person confirmation by policy. Everyone
    else is never named -- their name is typically already spoken in the transcript, and the point
    is to decline to manufacture a durable, searchable speaker identity for a private citizen.
    """
    if candidate.tier == TIER_OTHER:
        return NamingDecision(candidate, publish=False, needs_review=False, reason="tier-other")
    if not meets_agreement_rule(candidate.signals, candidate.tier):
        return NamingDecision(
            candidate, publish=False, needs_review=False, reason="insufficient-agreement"
        )
    if candidate.tier == TIER_MEMBER:
        established = _norm(candidate.display_name) in {
            _norm(name) for name in confirmed_names
        } or table.person_established(
            candidate.display_name,
            city_slug=city_slug,
            body=body,
            min_verdicts=min_member_verdicts,
            min_precision=min_precision,
        )
        if not established:
            return NamingDecision(
                candidate, publish=False, needs_review=True, reason="member-awaiting-confirmation"
            )
    key = candidate.combination_key
    if not table.trusted(key, min_verdicts=min_verdicts, min_precision=min_precision):
        # Fail-closed cold start: nothing publishes until the combination has evidence.
        return NamingDecision(
            candidate, publish=False, needs_review=True, reason="combination-untrusted"
        )
    if table.city_diverges(key, city_slug=city_slug):
        return NamingDecision(candidate, publish=False, needs_review=True, reason="city-divergence")
    reason = "member-established" if candidate.tier == TIER_MEMBER else "combination-trusted"
    return NamingDecision(candidate, publish=True, needs_review=False, reason=reason)


__all__ = [
    "DEFAULT_CITY_DIVERGENCE_MARGIN",
    "DEFAULT_CITY_MIN_VERDICTS",
    "DEFAULT_MIN_MEMBER_VERDICTS",
    "DEFAULT_MIN_PRECISION",
    "DEFAULT_MIN_VERDICTS",
    "ORIGINATING_SIGNALS",
    "SIGNAL_CHAIR_CUE",
    "SIGNAL_MEMBERSHIP",
    "SIGNAL_ROSTER",
    "SIGNAL_SELF_INTRO",
    "SIGNAL_TITLE_CUE",
    "SIGNAL_VOICE_PRINT",
    "UNTIMED_SIGNALS",
    "FusedCandidate",
    "NameProposal",
    "NamingDecision",
    "PrecisionTable",
    "decide",
    "fuse_proposals",
    "meets_agreement_rule",
]
