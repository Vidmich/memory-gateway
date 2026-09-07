"""The permission matrix, asserted cell by cell.

Task 04 asks for every role's row to be verified by a test rather than by reading the
table. So the expectations below are written out longhand, from SPEC §5.2, rather than
derived from ``ROLE_CAPABILITIES`` — a test that computes its expectation from the code
under test only proves the code is self-consistent.
"""

from __future__ import annotations

import pytest

from app.db.models.user import ROLES
from app.services.permissions import (
    ROLE_CAPABILITIES,
    Capability,
    allows,
    capabilities_for,
    capability_names,
)

#: SPEC §5.2, transcribed. Rows are capabilities, columns are roles.
MATRIX: dict[Capability, dict[str, bool]] = {
    Capability.ORG_READ: {
        "superadmin": True,
        "org_admin": True,
        "org_member": True,
        "org_viewer": True,
    },
    Capability.RESOURCES_WRITE: {
        "superadmin": True,
        "org_admin": True,
        "org_member": True,
        "org_viewer": False,
    },
    Capability.KEYS_MANAGE: {
        "superadmin": True,
        "org_admin": True,
        "org_member": False,
        "org_viewer": False,
    },
    Capability.ORG_ADMINISTER: {
        "superadmin": True,
        "org_admin": True,
        "org_member": False,
        "org_viewer": False,
    },
    Capability.PLATFORM_ADMINISTER: {
        "superadmin": True,
        "org_admin": False,
        "org_member": False,
        "org_viewer": False,
    },
}


@pytest.mark.parametrize(
    ("capability", "role", "expected"),
    [
        (capability, role, expected)
        for capability, row in MATRIX.items()
        for role, expected in row.items()
    ],
    ids=lambda value: str(value),
)
def test_every_cell_of_the_matrix(capability: Capability, role: str, expected: bool) -> None:
    assert allows(role, capability) is expected


def test_the_matrix_covers_every_capability() -> None:
    """A capability added without a row here would otherwise be tested by nothing."""
    assert set(MATRIX) == set(Capability)


def test_the_matrix_covers_every_role() -> None:
    for row in MATRIX.values():
        assert set(row) == set(ROLES)


def test_every_role_in_the_model_has_an_entry() -> None:
    """Widening the ``role`` CHECK without widening the table would silently give the new
    role no permissions — which is safe, but should be a deliberate choice."""
    assert set(ROLE_CAPABILITIES) == set(ROLES)


def test_an_unknown_role_gets_nothing() -> None:
    assert capabilities_for("root") == frozenset()
    assert not allows("", Capability.ORG_READ)


def test_a_superadmin_has_everything() -> None:
    assert capabilities_for("superadmin") == frozenset(Capability)


def test_names_are_sorted_and_stringly_typed() -> None:
    """`/auth/me` sends these to the browser, so they have to be plain strings and stable
    between requests."""
    names = capability_names("org_admin")
    assert names == sorted(names)
    assert all(isinstance(name, str) and not isinstance(name, Capability) for name in names)
    assert "org:administer" in names


def test_a_viewer_can_only_read() -> None:
    assert capability_names("org_viewer") == ["org:read"]
