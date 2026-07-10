"""Unit tests for the provider registry (citypods/providers/__init__.py)."""

from __future__ import annotations

import pytest

from citypods.providers import _REGISTRY, ProviderError, get_provider, register


class _FakeProvider:
    name = "registry-test-fake"

    def validate(self, source: dict) -> None:
        pass


def test_register_then_get_provider_round_trips():
    provider = _FakeProvider()
    register(provider)
    try:
        assert get_provider("registry-test-fake") is provider
    finally:
        _REGISTRY.pop("registry-test-fake", None)


def test_register_duplicate_name_raises_instead_of_silently_overwriting():
    # CR2-CP-29: a copy-paste/typo registering the same name twice used to silently overwrite
    # the earlier entry instead of surfacing the collision.
    register(_FakeProvider())
    try:
        with pytest.raises(ProviderError, match="already registered"):
            register(_FakeProvider())
    finally:
        _REGISTRY.pop("registry-test-fake", None)


def test_register_same_instance_twice_is_idempotent():
    provider = _FakeProvider()
    register(provider)
    try:
        register(provider)  # no-op, not a collision
        assert get_provider("registry-test-fake") is provider
    finally:
        _REGISTRY.pop("registry-test-fake", None)
