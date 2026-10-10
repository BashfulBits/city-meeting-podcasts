"""Per-entry independence: a judge never shares a family with the entry it judges (review/49 4c)."""

from __future__ import annotations

from collections.abc import Mapping

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


def sibling_for(
    producer_model: str | None, sibling: LaneConfig, families: Mapping[str, str]
) -> str | None:
    """The first active sibling model whose family differs from the producer's.

    Slots are tried in order (``google`` before ``non_google``); within a slot, active models in
    lane order. Returns None only if no active sibling is independent of the producer.
    """
    producer = family_of(producer_model, families)
    if producer == UNKNOWN_FAMILY:
        return None
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
    if UNKNOWN_FAMILY in excluded:
        return None
    if sibling_model:
        excluded.add(family_of(sibling_model, families))
    for model in adjudicator.models:
        if families.get(model) not in excluded:
            return model
    return None
