"""Tests for the R7 naming gate (review/31 §C.4).

These read as the policy itself: each test pins one rule that was settled in review, so a later
"simplification" that quietly changes who gets named fails here rather than in production.
"""

from __future__ import annotations

import pytest

from citypods.naming import (
    SIGNAL_CHAIR_CUE,
    SIGNAL_ROSTER,
    SIGNAL_SELF_INTRO,
    SIGNAL_TITLE_CUE,
    SIGNAL_VOICE_PRINT,
    FusedCandidate,
    NameProposal,
    PrecisionTable,
    decide,
    fuse_proposals,
    meets_agreement_rule,
)
from citypods.speakers import (
    TIER_MEMBER,
    TIER_OTHER,
    TIER_STAFF,
    classify_speaker_tier,
)

# --- Tier classification (§C.4.2) ---------------------------------------------------------------


def test_roster_section_is_authoritative_over_title_cues():
    roster = [{"name": "Jane Doe", "section": "Staff Present"}]
    # Even with an elected-sounding cue, an explicit staff section wins.
    assert classify_speaker_tier("Jane Doe", roster=roster, cue_kinds=[]) == TIER_STAFF


def test_flat_roster_falls_back_to_title_vocabulary():
    """The failure this fallback exists for: many cities publish one `Present:` list that mixes
    the Clerk and Attorney in with members."""
    roster = [{"name": "Matt Bodine"}, {"name": "Jane Doe"}]
    assert classify_speaker_tier("Matt Bodine", roster=roster, cue_kinds=["name-then-title"]) == (
        TIER_STAFF
    )
    assert classify_speaker_tier("Jane Doe", roster=roster, cue_kinds=["title-announcement"]) == (
        TIER_MEMBER
    )


def test_unsectioned_roster_hit_alone_still_means_member():
    assert classify_speaker_tier("Jane Doe", roster=[{"name": "Jane Doe"}]) == TIER_MEMBER


def test_conflicting_cues_resolve_to_the_member_tier():
    """Uncertainty must buy *more* scrutiny, never less: the member tier is the one that requires
    human confirmation, so a tie goes there."""
    tier = classify_speaker_tier(
        "Jane Doe", roster=[], cue_kinds=["title-announcement", "name-then-title"]
    )
    assert tier == TIER_MEMBER


def test_unknown_speaker_is_other():
    assert classify_speaker_tier("Someone Unlisted", roster=[], cue_kinds=[]) == TIER_OTHER
    assert classify_speaker_tier("", roster=[], cue_kinds=[]) == TIER_OTHER


# --- Agreement rule (§C.4.4) --------------------------------------------------------------------


def test_two_signals_with_an_originator_is_the_baseline():
    assert meets_agreement_rule([SIGNAL_CHAIR_CUE, SIGNAL_ROSTER], TIER_MEMBER)
    assert meets_agreement_rule([SIGNAL_VOICE_PRINT, SIGNAL_SELF_INTRO], TIER_MEMBER)


def test_roster_corroborates_but_can_never_name_anyone_alone():
    assert not meets_agreement_rule([SIGNAL_ROSTER], TIER_MEMBER)
    # Roster plus a signal that doesn't count for this tier is still just one countable signal.
    assert not meets_agreement_rule([SIGNAL_ROSTER, SIGNAL_TITLE_CUE], TIER_MEMBER)


def test_a_single_originating_signal_is_not_enough():
    assert not meets_agreement_rule([SIGNAL_CHAIR_CUE], TIER_MEMBER)
    assert not meets_agreement_rule([SIGNAL_SELF_INTRO], TIER_STAFF)


def test_title_cue_counts_as_the_second_signal_for_staff_only():
    """The staff exception exists because staff often have no roster entry and no voice print, so
    a flat two-signal rule would silently mean 'staff are never named'."""
    assert meets_agreement_rule([SIGNAL_SELF_INTRO, SIGNAL_TITLE_CUE], TIER_STAFF)
    # The same pair must NOT let a member through -- members keep the stricter rule.
    assert not meets_agreement_rule([SIGNAL_SELF_INTRO, SIGNAL_TITLE_CUE], TIER_MEMBER)


# --- Fusion (§C.3) ------------------------------------------------------------------------------


def test_fusion_collapses_agreeing_signals_into_one_candidate():
    """A reviewer sees one best-supported suggestion, not several half-signals each wanting its
    own look."""
    proposals = [
        NameProposal(cluster="c1", display_name="Jane Doe", signal=SIGNAL_CHAIR_CUE),
        NameProposal(cluster="c1", display_name="jane doe", signal=SIGNAL_ROSTER),
        NameProposal(cluster="c2", display_name="Matt Bodine", signal=SIGNAL_SELF_INTRO),
    ]
    fused = fuse_proposals(proposals, tier_of=lambda _name: TIER_MEMBER)
    by_cluster = {item.cluster: item for item in fused}
    assert set(by_cluster) == {"c1", "c2"}
    assert by_cluster["c1"].signals == (SIGNAL_CHAIR_CUE, SIGNAL_ROSTER)
    assert by_cluster["c1"].display_name == "Jane Doe"  # keeps the first real spelling
    assert by_cluster["c2"].signals == (SIGNAL_SELF_INTRO,)


def test_combination_key_includes_the_tier():
    """The same combination differs in reliability by tier -- roster corroboration is strong for a
    member (on the roster by definition) and weak for staff (often not) -- so they must not share
    a precision bucket."""
    signals = (SIGNAL_CHAIR_CUE, SIGNAL_ROSTER)
    member = FusedCandidate("c", "Jane Doe", TIER_MEMBER, signals)
    staff = FusedCandidate("c", "Jane Doe", TIER_STAFF, signals)
    assert member.combination_key != staff.combination_key


# --- Precision table + gate (§C.4.4-C.4.6) -------------------------------------------------------


def _staff_candidate() -> FusedCandidate:
    return FusedCandidate("c1", "Matt Bodine", TIER_STAFF, (SIGNAL_SELF_INTRO, SIGNAL_TITLE_CUE))


def test_members_always_need_human_confirmation_however_strong_the_signals():
    table = PrecisionTable()
    candidate = FusedCandidate(
        "c1", "Jane Doe", TIER_MEMBER, (SIGNAL_CHAIR_CUE, SIGNAL_ROSTER, SIGNAL_VOICE_PRINT)
    )
    for _ in range(500):  # overwhelming history must not buy a member an automatic name
        table.record(candidate.combination_key, city_slug="denton-tx", agreed=True)
    decision = decide(candidate, table, city_slug="denton-tx")
    assert not decision.publish
    assert decision.needs_review
    assert decision.reason == "member-awaiting-confirmation"


def test_a_confirmed_member_publishes():
    table = PrecisionTable()
    candidate = FusedCandidate("c1", "Jane Doe", TIER_MEMBER, (SIGNAL_CHAIR_CUE, SIGNAL_ROSTER))
    decision = decide(candidate, table, city_slug="denton-tx", confirmed_names=["Jane Doe"])
    assert decision.publish
    assert not decision.needs_review


def test_other_tier_is_never_named_and_never_queued():
    table = PrecisionTable()
    candidate = FusedCandidate(
        "c1", "A Commenter", TIER_OTHER, (SIGNAL_SELF_INTRO, SIGNAL_VOICE_PRINT)
    )
    decision = decide(candidate, table, city_slug="denton-tx")
    assert not decision.publish
    assert not decision.needs_review  # costs no review effort, by design
    assert decision.reason == "tier-other"


def test_cold_start_is_fail_closed_for_staff():
    """Nothing publishes until the combination has evidence -- the retracted alternative
    (auto-admit on strong agreement) would have required a retraction path."""
    decision = decide(_staff_candidate(), PrecisionTable(), city_slug="denton-tx")
    assert not decision.publish
    assert decision.needs_review
    assert decision.reason == "combination-untrusted"


def test_staff_publishes_once_the_combination_earns_trust():
    table = PrecisionTable()
    candidate = _staff_candidate()
    for _ in range(20):  # exactly the threshold, all agreeing
        table.record(candidate.combination_key, city_slug="denton-tx", agreed=True)
    decision = decide(candidate, table, city_slug="denton-tx")
    assert decision.publish
    assert decision.reason == "combination-trusted"


def test_trust_needs_both_enough_verdicts_and_enough_precision():
    table = PrecisionTable()
    candidate = _staff_candidate()
    key = candidate.combination_key
    for _ in range(19):
        table.record(key, city_slug="denton-tx", agreed=True)
    assert not table.trusted(key)  # 19 < 20 verdicts
    table.record(key, city_slug="denton-tx", agreed=True)
    assert table.trusted(key)
    # Precision below the bar revokes it even with plenty of samples.
    for _ in range(3):
        table.record(key, city_slug="denton-tx", agreed=False)
    assert table.precision(key) < 0.95
    assert not table.trusted(key)


def test_global_pooling_lets_a_new_city_start_trusted():
    """The point of pooling: city #2 inherits the trust city #1 earned, instead of re-earning it."""
    table = PrecisionTable()
    candidate = _staff_candidate()
    for _ in range(20):
        table.record(candidate.combination_key, city_slug="denton-tx", agreed=True)
    decision = decide(candidate, table, city_slug="brand-new-city")
    assert decision.publish


def test_city_divergence_guardrail_pulls_a_bad_city_back_to_review():
    """A city with genuinely worse audio must not inherit optimism it hasn't earned."""
    table = PrecisionTable()
    candidate = _staff_candidate()
    key = candidate.combination_key
    for _ in range(200):
        table.record(key, city_slug="denton-tx", agreed=True)
    for index in range(20):  # a second city doing much worse than the global prior
        table.record(key, city_slug="rough-audio-tx", agreed=index % 2 == 0)
    assert decide(candidate, table, city_slug="denton-tx").publish
    bad = decide(candidate, table, city_slug="rough-audio-tx")
    assert not bad.publish
    assert bad.reason == "city-divergence"


def test_a_couple_of_unlucky_verdicts_do_not_revoke_a_new_citys_trust():
    """Divergence needs a minimum local sample: two bad calls should not undo a thousand good
    ones elsewhere."""
    table = PrecisionTable()
    candidate = _staff_candidate()
    key = candidate.combination_key
    for _ in range(200):
        table.record(key, city_slug="denton-tx", agreed=True)
    table.record(key, city_slug="brand-new-city", agreed=False)
    table.record(key, city_slug="brand-new-city", agreed=False)
    assert decide(candidate, table, city_slug="brand-new-city").publish


def test_table_is_rebuilt_from_the_review_ledger():
    """The table is derived, not stored: a verdict recorded against a candidate is the only thing
    that moves it, so there is no second copy of 'what humans decided' to drift."""
    key = "staff:self-introduction+title-cue"
    evaluation = {
        "naming_candidates": {
            "r7-name-a": {"combination_key": key, "city_slug": "denton-tx"},
            "r7-name-b": {"combination_key": key, "city_slug": "denton-tx"},
        },
        "reviews": [
            {"candidate_id": "r7-name-a", "correct": True},
            {"candidate_id": "r7-name-b", "correct": False},
        ],
    }
    table = PrecisionTable.from_evaluation(evaluation)
    assert table.verdicts(key) == 2
    assert table.precision(key) == pytest.approx(0.5)
    assert table.verdicts(key, city_slug="denton-tx") == 2


def test_verdicts_on_unknown_candidates_are_skipped_not_guessed():
    """Reviews predating the naming gate (or any trimmed ledger) name a candidate this table has
    never seen. Counting them somewhere would inflate a combination that did not earn it."""
    evaluation = {
        "naming_candidates": {"r7-name-a": {"combination_key": "staff:x", "city_slug": "d"}},
        "reviews": [
            {"candidate_id": "r7-name-a", "correct": True},
            {"candidate_id": "r7-ref-legacy", "correct": True},
            {"candidate_id": "", "correct": True},
            "not-a-row",
        ],
    }
    table = PrecisionTable.from_evaluation(evaluation)
    assert table.verdicts("staff:x") == 1


def test_missing_or_malformed_ledger_yields_an_empty_table():
    assert PrecisionTable.from_evaluation({}).verdicts("anything") == 0
    assert PrecisionTable.from_evaluation({"naming_candidates": []}).verdicts("anything") == 0
