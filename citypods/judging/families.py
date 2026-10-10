"""Per-entry independence: a judge never shares a family with the entry it judges (review/49 4c)."""

from __future__ import annotations

from collections.abc import Mapping

from citypods.compute.llm_lanes import LaneConfig

RULE_FAMILY = "rule"


def family_of(model: str | None, families: Mapping[str, str]) -> str:
    """The family of a producer or judge model; rule candidates (no model) are ``rule``."""
    if not model or str(model).startswith("rule:"):
        return RULE_FAMILY
    try:
        return families[model]
    except KeyError:
        raise ValueError(f"no llm_families entry for model {model!r}") from None


def sibling_for(
    producer_model: str | None, sibling: LaneConfig, families: Mapping[str, str]
) -> str | None:
    """The first active sibling model whose family differs from the producer's.

    Slots are tried in order (``google`` before ``non_google``); within a slot, active models in
    lane order. Returns None only if no active sibling is independent of the producer.
    """
    producer = family_of(producer_model, families)
    slots = sibling.slot_models or {"all": sibling.models}
    for name in sorted(slots):
        for model in slots[name]:
            if model in sibling.models and families.get(model) != producer:
                return model
    return None


def adjudicator_for(
    producer_model: str | None,
    sibling_model: str | None,
    adjudicator: LaneConfig,
    families: Mapping[str, str],
) -> str | None:
    """The first active adjudicator independent of both the producer and the sibling (PR5)."""
    excluded = {family_of(producer_model, families)}
    if sibling_model:
        excluded.add(family_of(sibling_model, families))
    for model in adjudicator.models:
        if families.get(model) not in excluded:
            return model
    return None
