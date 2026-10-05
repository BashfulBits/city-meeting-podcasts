"""Live contract for the quality catalog used by provider reconciliation."""

import os

import pytest
import requests

from citypods.provider_catalog.quality import AA_KEY_ENV, FLOOR_MODELS, fetch_quality_index

pytestmark = pytest.mark.live


def test_artificial_analysis_quality_catalog():
    # This integration is configured in production. Missing CI credentials must fail visibly
    # rather than silently skipping the very contract the scheduled job promises to check.
    assert os.environ.get(AA_KEY_ENV), f"{AA_KEY_ENV} must be configured for this contract"
    with requests.Session() as session:
        quality = fetch_quality_index(session)
    assert quality.error is None, quality.error
    assert quality.scores, "Artificial Analysis returned no usable Intelligence Index scores"
    for identity in FLOOR_MODELS:
        assert identity in quality.scores, (
            f"Artificial Analysis reference model missing: {identity}"
        )
    assert quality.floor is not None, "Artificial Analysis quality floor could not be computed"
