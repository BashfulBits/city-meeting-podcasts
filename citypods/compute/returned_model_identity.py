"""Evidence-bound returned model names for a specific physical provider route.

An accepted name is an identity signal, not proof of the provider's serving weights or billing.
Unknown names remain unverified; no prefix stripping or cross-route alias inference is performed.
"""

from __future__ import annotations

from typing import Literal

IdentityStatus = Literal["exact", "alias", "unknown"]

# The maintainer-approved report-only / route-admission exception in review/51. A model-bound
# P3 admission deliberately does not use returned-name checking; this registry does not alter it.
RETURNED_MODEL_ALIASES: dict[tuple[str, str], tuple[str, ...]] = {
    ("orcarouter_zai_glm_5_3_flash_free", "z-ai/glm-5.3-flash-free"): ("zai-org/glm-5.3-flash",),
}


def accepted_returned_models(route_id: str, requested: str) -> tuple[str, ...]:
    """Return exact upstream and explicitly reviewed aliases for this physical route."""
    return (requested, *RETURNED_MODEL_ALIASES.get((route_id, requested), ()))


def returned_model_status(
    route_id: str, requested: str, direct_model: str, returned: object
) -> IdentityStatus:
    """Classify a returned name; an absent name and all unverified variants remain unknown."""
    if not isinstance(returned, str) or not returned:
        return "unknown"
    if returned in (requested, direct_model):
        return "exact"
    if returned in RETURNED_MODEL_ALIASES.get((route_id, requested), ()):
        return "alias"
    return "unknown"
