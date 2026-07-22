"""Shared authorization policy for maintainer commands received through GitHub issues."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RepositoryPermissionError(ValueError):
    """The command actor does not have repository write permission."""


_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "push", "write"})


def require_repository_write(permission: Mapping[str, Any]) -> str:
    """Return the normalized permission when GitHub confirms write-or-higher access.

    The collaborators permission endpoint has used both ``push`` and ``write`` vocabulary across
    API surfaces.  ``role_name`` also carries the base role for custom organization roles, so
    accept either field while failing closed on missing or unfamiliar values.
    """
    values = {
        str(permission.get("permission") or "").strip().lower(),
        str(permission.get("role_name") or "").strip().lower(),
    }
    granted = next((value for value in values if value in _WRITE_PERMISSIONS), None)
    if granted is None:
        raise RepositoryPermissionError(
            "maintainer commands require repository write, maintain, or admin permission"
        )
    return granted
