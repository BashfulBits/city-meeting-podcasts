"""Import every script module so a stale/broken import is caught in CI.

`scripts/generate_board_cities.py` shipped with a dead `citypods.media._source_key` import
after the record refactor and would only fail when run. Importing each script here makes that
class of breakage a unit-test failure instead of a runtime surprise.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import SimpleNamespace

import pytest
import yaml

import scripts

SCRIPT_MODULES = [f"scripts.{m.name}" for m in pkgutil.iter_modules(scripts.__path__)]


@pytest.mark.parametrize("module", SCRIPT_MODULES)
def test_script_imports(module):
    importlib.import_module(module)


def test_found_the_scripts():
    # Guard against the discovery silently finding nothing (e.g. missing __init__).
    assert any(m.endswith("generate_board_cities") for m in SCRIPT_MODULES)


def test_generate_board_cities_render_yaml_escapes_scalars():
    from scripts.generate_board_cities import _render

    tmpl = SimpleNamespace(
        city_entity='Denton "Metro"',
        provider="swagit",
        source={"list_url": "https://example.test/a?x=1&y=2", "body": "old"},
        podcast_author='Author "Name"',
        podcast_email="podcasts@example.test",
        max_episodes=25,
        extract_audio=True,
    )
    body = 'Board "A"\nPlanning: North'
    title_prefix = 'Denton \\ Civic'

    rendered = _render(tmpl, "denton-board-a", body, title_prefix)
    loaded = yaml.safe_load(rendered)

    assert loaded["city"] == tmpl.city_entity
    assert loaded["source"] == {
        "list_url": "https://example.test/a?x=1&y=2",
        "body": body,
    }
    assert loaded["podcast_title"] == f"{title_prefix}: {body}"
    assert loaded["podcast_author"] == tmpl.podcast_author
    assert loaded["podcast_description"] == f"{body} meetings for {title_prefix}."
    assert loaded["max_episodes"] == 25
    assert loaded["extract_audio"] is True
