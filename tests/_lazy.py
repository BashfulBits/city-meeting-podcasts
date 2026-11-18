"""Lazy module proxies, so collecting tests never imports torch or transformers.

Some tests replace ``sys.modules["torch"]`` with a stub and pop it afterwards; a real torch imported
during collection would then be re-imported and break. These tests import on first use instead.
"""

import importlib

import pytest


class Lazy:
    def __init__(self, name: str):
        self._name = name

    def _module(self):
        return importlib.import_module(self._name)

    def __getattr__(self, attribute):
        if attribute.startswith("_"):  # pytest probes dunders while collecting; do not import
            raise AttributeError(attribute)
        return getattr(self._module(), attribute)


def require(*names: str) -> None:
    """Skip the calling test when a module is missing; call from an autouse fixture."""
    for name in names:
        pytest.importorskip(name)
