"""Live endpoint contract tests (opt-in: ``pytest -m live``).

These hit real provider URLs through the existing adapters and assert the minimal shape the
pipeline depends on, so a platform's HTML/JSON change surfaces as a named failure here instead of
silently degrading feeds. Excluded from the default offline suite (see pyproject ``addopts``);
run in the scheduled ``contracts.yml`` workflow.
"""

from __future__ import annotations

import pytest

from citypods.config import load_city_configs, load_site_config
from citypods.contracts import check_city, representative_cities

pytestmark = pytest.mark.live


def _representatives():
    sc = load_site_config("config/site_config.yml")
    cities = load_city_configs("config", sc.get("defaults", {}))
    return representative_cities(cities)


def pytest_generate_tests(metafunc):
    # `-m 'not live'` (pyproject addopts) is a post-collection filter, so a plain
    # @pytest.mark.parametrize(..., _representatives()) would still run config loading at
    # collection time on every pytest invocation, live or not. Skip it unless live tests are
    # actually selected.
    if "city" not in metafunc.fixturenames:
        return
    if metafunc.config.getoption("markexpr") == "not live":
        metafunc.parametrize("city", [], ids=[])
        return
    metafunc.parametrize("city", _representatives(), ids=lambda c: c.provider)


def test_provider_contracts(city):
    results = check_city(city.slug, city.provider, city.source)
    failures = [r for r in results if not r.ok]
    assert results, f"no contract checks ran for {city.provider}"
    assert not failures, "broken endpoint(s): " + "; ".join(
        f"{r.endpoint} ({r.detail})" for r in failures
    )
