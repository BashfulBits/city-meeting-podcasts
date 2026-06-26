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


def _live_selected(markexpr: str) -> bool:
    """True unless *markexpr* would deselect the ``live`` mark.

    Uses pytest's own mark-expression evaluator (the same one ``-m`` filtering runs later)
    rather than string-matching the literal ``"not live"`` default, so any expression that
    also happens to exclude ``live`` (e.g. ``"not (live)"``, ``"unit"``) is caught too.
    """
    if not markexpr:
        return True
    from _pytest.mark.expression import Expression

    try:
        expr = Expression.compile(markexpr)
    except Exception:
        return True  # can't evaluate it safely -- don't skip work we can't be sure is unwanted
    return expr.evaluate(lambda name: name == "live")


def pytest_generate_tests(metafunc):
    # `-m 'not live'` (pyproject addopts) is a post-collection filter, so a plain
    # @pytest.mark.parametrize(..., _representatives()) would still run config loading at
    # collection time on every pytest invocation, live or not. Skip it unless live tests are
    # actually selected.
    if "city" not in metafunc.fixturenames:
        return
    if not _live_selected(metafunc.config.getoption("markexpr")):
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
