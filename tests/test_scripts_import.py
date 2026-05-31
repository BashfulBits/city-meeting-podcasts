"""Import every script module so a stale/broken import is caught in CI.

`scripts/generate_board_cities.py` shipped with a dead `citypods.media._source_key` import
after the record refactor and would only fail when run. Importing each script here makes that
class of breakage a unit-test failure instead of a runtime surprise.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import scripts

SCRIPT_MODULES = [f"scripts.{m.name}" for m in pkgutil.iter_modules(scripts.__path__)]


@pytest.mark.parametrize("module", SCRIPT_MODULES)
def test_script_imports(module):
    importlib.import_module(module)


def test_found_the_scripts():
    # Guard against the discovery silently finding nothing (e.g. missing __init__).
    assert any(m.endswith("generate_board_cities") for m in SCRIPT_MODULES)
