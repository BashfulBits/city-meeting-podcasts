import pytest

from citypods.github_permissions import RepositoryPermissionError, require_repository_write


@pytest.mark.parametrize("permission", ["write", "push", "maintain", "admin"])
def test_repository_write_permission_accepts_write_or_higher(permission):
    assert require_repository_write({"permission": permission}) == permission


def test_repository_write_permission_accepts_custom_role_with_write_base():
    assert require_repository_write({"permission": "custom", "role_name": "write"}) == "write"


@pytest.mark.parametrize("permission", ["", "none", "read", "triage", "custom"])
def test_repository_write_permission_fails_closed(permission):
    with pytest.raises(RepositoryPermissionError, match="repository write"):
        require_repository_write({"permission": permission, "role_name": permission})
