"""Verified route aliases are narrow and never attest to serving weights."""

from citypods.compute.returned_model_identity import (
    accepted_returned_models,
    returned_model_status,
)

ROUTE = "orcarouter_zai_glm_5_3_flash_free"
REQUESTED = "z-ai/glm-5.3-flash-free"
DIRECT = "orcarouter/z-ai/glm-5.3-flash-free"
ALIAS = "zai-org/glm-5.3-flash"


def test_identity_accepts_only_the_reviewed_alias_on_its_physical_route():
    assert returned_model_status(ROUTE, REQUESTED, DIRECT, REQUESTED) == "exact"
    assert returned_model_status(ROUTE, REQUESTED, DIRECT, DIRECT) == "exact"
    assert returned_model_status(ROUTE, REQUESTED, DIRECT, ALIAS) == "alias"
    assert ALIAS in accepted_returned_models(ROUTE, REQUESTED)
    assert returned_model_status("another-route", REQUESTED, DIRECT, ALIAS) == "unknown"
    assert returned_model_status(ROUTE, "z-ai/glm-5.3", DIRECT, ALIAS) == "unknown"


def test_unreviewed_response_names_and_missing_values_stay_unknown():
    for value in (None, "", "glm-5.3-flash", "zai-org/glm-5.3", "zai-org/glm-5.4-flash"):
        assert returned_model_status(ROUTE, REQUESTED, DIRECT, value) == "unknown"
