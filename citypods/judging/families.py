"""Per-entry independence: a judge never shares a family with the entry it judges (review/49 4c)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from citypods.compute.llm_lanes import LaneConfig

RULE_FAMILY = "rule"


UNKNOWN_FAMILY = "unknown"


def normalize_model(model: str) -> str:
    """The logical model key behind a stored producer identity.

    Candidates record producers as the backend saw them: tag LLM candidates as
    ``litellm:gemini/gemini-3.1-flash-lite`` and moment candidates as the resolved (possibly
    route-specific) model. Strip a ``<backend>:`` prefix, then resolve aliases to the logical key.
    """
    from citypods.compute.llm_policy import canonical_model

    head, sep, rest = model.partition(":")
    if sep and "/" not in head and rest:
        model = rest
    return canonical_model(model)


def family_of(model: str | None, families: Mapping[str, str]) -> str:
    """The family of a producer or judge model; rule candidates (no model) are ``rule``.

    A model with no ``llm_families`` entry, even after normalizing, is ``unknown``: no judge can be
    shown independent of it, so it gets no sibling or adjudicator (see ``sibling_for``) and is
    counted, never silently paired with a judge of possibly the same family.
    """
    if not model or str(model).startswith("rule:"):
        return RULE_FAMILY
    if model in families:
        return families[model]
    return families.get(normalize_model(str(model)), UNKNOWN_FAMILY)


def anchor_family(anchor_model: str | None, families: Mapping[str, str]) -> str | None:
    """The anchor judge's family, which a sibling or adjudicator must not share (review/58 WP9).

    JEV (``typesafe``) shared no family with any producer or sibling, so the rule was never
    exercised; Gemini Decision 1 is ``google``, like the Gemini producers and the Gemma sibling."""
    if not anchor_model:
        return None
    return family_of(anchor_model, families)


def anchor_independent_of(
    producer_models: Sequence[str | None], anchor_model: str, families: Mapping[str, str]
) -> bool:
    """Whether the anchor may judge entries of these producers: never one of its own family.

    An unknown producer family cannot be shown independent, so it gets no anchor either (the same
    rule ``sibling_candidates`` applies). A rule producer is independent of every model."""
    anchor = family_of(anchor_model, families)
    if anchor == UNKNOWN_FAMILY:
        return False
    for model in producer_models:
        producer = family_of(model, families)
        if producer == UNKNOWN_FAMILY or producer == anchor:
            return False
    return True


def sibling_candidates(
    producer_model: str | None,
    sibling: LaneConfig,
    families: Mapping[str, str],
    *,
    allow_all_slots: bool = False,
    exclude_families: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Every active sibling of the first slot that has one independent of the producer.

    Slots are tried in order (``google`` before ``non_google``); within the slot, active models in
    lane order. Empty only if no active sibling is independent of the producer (or the producer's
    family is unknown). A slot is a pool of interchangeable judges, so any of them may be chosen
    by load (``runner._pick_sibling``); a later slot is used only when the earlier one has none.
    When ``allow_all_slots`` is True, all independent active models across all slots are returned,
    allowing load-balancing across slots (e.g. for rule-produced subjects with no LLM conflict).
    ``exclude_families`` are the other judges' families (the anchor's): a sibling shares none.
    """
    producer = family_of(producer_model, families)
    if producer == UNKNOWN_FAMILY:
        return ()
    excluded = {producer, *exclude_families}
    slots = sibling.slot_models or {"all": sibling.models}
    if allow_all_slots:
        return tuple(model for model in sibling.models if families.get(model) not in excluded)
    for name in sorted(slots):
        found = tuple(
            model
            for model in slots[name]
            if model in sibling.models and families.get(model) not in excluded
        )
        if found:
            return found
    return ()


def sibling_for(
    producer_model: str | None,
    sibling: LaneConfig,
    families: Mapping[str, str],
    *,
    exclude_families: frozenset[str] = frozenset(),
) -> str | None:
    """The first sibling candidate: the stable default when load is unknown."""
    found = sibling_candidates(producer_model, sibling, families, exclude_families=exclude_families)
    return found[0] if found else None


def choose_sibling_candidates(
    producer_models: Sequence[str | None],
    sibling: LaneConfig,
    families: Mapping[str, str],
    *,
    allow_all_slots: bool = False,
    exclude_families: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Siblings independent of every producer in a meeting, from the first slot that has one."""
    producer_families = {family_of(model, families) for model in producer_models}
    if UNKNOWN_FAMILY in producer_families:
        return ()
    excluded = producer_families | set(exclude_families)
    independent = [m for m in sibling.models if families.get(m) not in excluded]
    if not independent:
        return ()
    if allow_all_slots:
        return tuple(independent)
    slots = sibling.slot_models or {}

    def slot_of(model: str) -> str:
        return next((name for name in sorted(slots) if model in slots[name]), "")

    first = slot_of(independent[0])
    return tuple(model for model in independent if slot_of(model) == first)


def adjudicator_for(
    producer_model: str | None,
    sibling_model: str | None,
    adjudicator: LaneConfig,
    families: Mapping[str, str],
    *,
    exclude_families: frozenset[str] = frozenset(),
) -> str | None:
    """The first active adjudicator independent of the producer, the sibling and the anchor (PR5;
    the anchor's family arrives in ``exclude_families``)."""
    excluded = {family_of(producer_model, families)}
    if UNKNOWN_FAMILY in excluded:
        return None
    if sibling_model:
        excluded.add(family_of(sibling_model, families))
    excluded.update(exclude_families)
    for model in adjudicator.models:
        if families.get(model) not in excluded:
            return model
    return None
