"""What each role may do, as data.

The alternative — ``if user.role == "org_admin"`` scattered through forty endpoints — is
how permission bugs get written: the checks drift, nobody can answer "what can a viewer
do?" without grepping, and a new endpoint inherits whatever its author remembered. Here
the matrix from SPEC §5.2 is one table, every row is asserted by a test, and an endpoint
names a *capability* rather than a list of roles.

Adding a capability means adding a row and deciding every cell. That is the point: the
compiler will not tell you a role was forgotten, but ``ROLE_CAPABILITIES`` being total
over both axes means the table itself does.
"""

from __future__ import annotations

from enum import StrEnum

from app.db.models.user import ROLES

__all__ = [
    "ROLES",
    "ROLE_CAPABILITIES",
    "Capability",
    "allows",
    "capabilities_for",
    "capability_names",
]


class Capability(StrEnum):
    """A thing a caller can be allowed to do.

    Deliberately coarse. Roles in this product are org-wide (SPEC, task 04 "out of
    scope": no per-resource ACLs), so a finer vocabulary would promise a precision the
    model does not have.
    """

    #: Read anything belonging to the organization in scope: gateways, models,
    #: connectors, logs, members, monitoring.
    ORG_READ = "org:read"
    #: Create and edit connectors, gateways, and the organization's own models.
    RESOURCES_WRITE = "resources:write"
    #: Create, reveal (once), and revoke data-plane API keys.
    KEYS_MANAGE = "keys:manage"
    #: Members, roles, invitations — and the organization's own profile and defaults,
    #: which live on the same Settings screen and have the same audience.
    ORG_ADMINISTER = "org:administer"
    #: Organizations themselves, the global model catalog, platform settings.
    PLATFORM_ADMINISTER = "platform:administer"


#: SPEC §5.2, one row per role. Total over ``ROLES`` — see ``test_permissions.py``.
ROLE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "superadmin": frozenset(Capability),
    "org_admin": frozenset(
        {
            Capability.ORG_READ,
            Capability.RESOURCES_WRITE,
            Capability.KEYS_MANAGE,
            Capability.ORG_ADMINISTER,
        }
    ),
    "org_member": frozenset({Capability.ORG_READ, Capability.RESOURCES_WRITE}),
    "org_viewer": frozenset({Capability.ORG_READ}),
}


def capabilities_for(role: str) -> frozenset[Capability]:
    """An unknown role gets nothing.

    A role that is not in the table is a bug — a migration that widened the CHECK without
    widening this — and the safe reading of a bug is "no permissions", not "all of them".
    """
    return ROLE_CAPABILITIES.get(role, frozenset())


def allows(role: str, capability: Capability) -> bool:
    return capability in capabilities_for(role)


def capability_names(role: str) -> list[str]:
    """Sorted, so ``/auth/me`` is stable and the UI can compare snapshots."""
    return sorted(str(capability) for capability in capabilities_for(role))
