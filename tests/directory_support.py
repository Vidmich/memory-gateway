"""A two-tenant world, built once and used by every tenancy test.

Two organizations with overlapping shapes is the minimum that can catch an isolation bug:
one organization alone makes every query look correctly scoped, because there is nothing
else it could have returned.

Everything runs against :class:`MemoryDatabase`, shared by the auth and directory stores,
so these tests need no PostgreSQL. ``tests/test_directory_db.py`` runs the contract
against the real one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.passwords import Hasher, build_hasher
from app.core.tenancy import Actor, TenantScope
from app.db.models import (
    ApiKey,
    Connector,
    Document,
    EndUser,
    EvaluationItem,
    EvaluationRun,
    EvaluationSet,
    Gateway,
    MemoryFact,
    Organization,
    ReprocessingRun,
    RequestLog,
    UpstreamModel,
    User,
)
from app.services.catalog import CatalogService
from app.services.directory import DirectoryService
from app.services.gateways import GatewayService
from app.services.memory_db import MemoryDatabase
from tests.auth_support import PASSWORD, AuthFixture, build_auth, make_organization, make_user
from tests.catalog_support import (
    ACME_SECRET,
    PLATFORM_SECRET,
    FakeProbe,
    make_model,
    make_target_row,
)
from tests.connector_support import make_connector, make_document
from tests.end_user_support import make_end_user, make_fact
from tests.gateway_support import (
    FakeGatewayProbe,
    RecordingCache,
    make_gateway_row,
    make_key_row,
)
from tests.monitoring_support import make_log_row
from tests.platform_support import PlatformFixture
from tests.platform_support import build_platform as build_platform_fixture
from tests.reprocessing_support import make_reprocessing_run
from tests.validation_support import (
    make_evaluation_item,
    make_evaluation_run,
    make_evaluation_set,
)


@dataclass
class World:
    """Two organizations, one of every role, a platform account, and a model each.

    The catalog half mirrors the directory half deliberately: two org models with
    different owners, one global model owned by nobody, and one gateway pointing at
    Acme's model so the referenced-delete guard has something real to refuse.
    """

    settings: Settings
    hasher: Hasher
    database: MemoryDatabase
    directory: DirectoryService
    catalog: CatalogService
    gateways: GatewayService
    probe: FakeProbe
    gateway_probe: FakeGatewayProbe
    cache: RecordingCache
    auth: AuthFixture
    #: Task 17's operator half, over the same rows. Here rather than in a fixture of
    #: its own because the audit tour and the cross-tenant net both drive the whole
    #: control plane, and a platform screen missing from the app they drive is a
    #: screen those guards silently stop covering.
    platform: PlatformFixture

    acme: Organization
    globex: Organization

    superadmin: User
    acme_admin: User
    acme_member: User
    acme_viewer: User
    globex_admin: User

    acme_model: UpstreamModel
    globex_model: UpstreamModel
    global_model: UpstreamModel
    acme_gateway: Gateway
    globex_gateway: Gateway
    acme_key: ApiKey
    globex_key: ApiKey
    #: One connector and one indexed document each, so the cross-tenant net can aim at
    #: content that genuinely exists rather than at invented ids.
    acme_connector: Connector
    globex_connector: Connector
    acme_document: Document
    globex_document: Document
    #: One request each, so the cross-tenant net can aim at a log row that exists.
    acme_log: RequestLog
    globex_log: RequestLog
    #: One end user and one fact each. Conversation memory is the most personal thing in
    #: the system, so the net needs a foreign end user and a foreign fact that genuinely
    #: exist rather than invented ids that would 404 for the wrong reason.
    acme_end_user: EndUser
    globex_end_user: EndUser
    acme_fact: MemoryFact
    globex_fact: MemoryFact
    #: Task 103. One evaluation set, item and run of Globex's, so the cross-tenant net can
    #: aim at labelled questions that genuinely exist.
    globex_evaluation_set: EvaluationSet
    globex_evaluation_item: EvaluationItem
    globex_evaluation_run: EvaluationRun
    globex_reprocessing_run: ReprocessingRun

    #: Every user, keyed by the short name the tests use.
    people: dict[str, User] = field(default_factory=dict)

    def actor(self, user: User) -> Actor:
        return Actor(user_id=user.id, scope=TenantScope.of_user(user))

    def platform_actor_assuming(self, organization_id: uuid.UUID) -> Actor:
        scope = TenantScope.of_user(self.superadmin)
        return Actor(
            user_id=self.superadmin.id,
            scope=scope.assume(organization_id, actor_user_id=self.superadmin.id),
        )


def build_world(*, settings: Settings | None = None) -> World:
    settings = settings or get_settings()
    hasher = build_hasher(settings)
    database = MemoryDatabase()

    acme = make_organization(name="Acme", slug="acme")
    globex = make_organization(name="Globex", slug="globex")
    database.add_organization(acme)
    database.add_organization(globex)

    people = {
        "superadmin": make_user(
            hasher=hasher, email="root@example.com", role="superadmin", organization=None
        ),
        "acme_admin": make_user(
            hasher=hasher, email="admin@acme.example.com", role="org_admin", organization=acme
        ),
        "acme_member": make_user(
            hasher=hasher, email="member@acme.example.com", role="org_member", organization=acme
        ),
        "acme_viewer": make_user(
            hasher=hasher, email="viewer@acme.example.com", role="org_viewer", organization=acme
        ),
        "globex_admin": make_user(
            hasher=hasher, email="admin@globex.example.com", role="org_admin", organization=globex
        ),
    }
    for person in people.values():
        database.add_user(person)

    # `build_auth` wires the login service to the same rows; the user it is "built
    # around" only matters for the convenience helpers on the fixture.
    auth = build_auth(
        settings=settings,
        hasher=hasher,
        database=database,
        user=people["acme_admin"],
        organization=acme,
    )

    acme_model = make_model(
        organization=acme,
        name="acme-gpt",
        credential=ACME_SECRET,
        secret_box=auth.secret_box,
    )
    globex_model = make_model(organization=globex, name="globex-gpt")
    global_model = make_model(
        organization=None,
        name="shared-gpt-4o",
        credential=PLATFORM_SECRET,
        secret_box=auth.secret_box,
        # An operator's private routing detail, and the sharpest reason a tenant must not
        # read a global model's headers: this one is an auth header.
        extra_headers={"x-operator-token": "operator-only-value"},
    )
    for model in (acme_model, globex_model, global_model):
        database.add_model(model)

    acme_gateway = make_gateway_row(acme, slug="acme-chat")
    database.add_gateway(acme_gateway)
    database.add_target(make_target_row(acme_gateway.id, acme_model.id))
    acme_key, _ = make_key_row(acme_gateway.id, name="acme production")
    database.add_key(acme_key)

    # Globex gets the mirror image, so the cross-tenant net has a real foreign gateway
    # and a real foreign key to aim at rather than invented ids.
    globex_gateway = make_gateway_row(globex, slug="globex-chat")
    database.add_gateway(globex_gateway)
    database.add_target(make_target_row(globex_gateway.id, globex_model.id))
    globex_key, _ = make_key_row(globex_gateway.id, name="globex production")
    database.add_key(globex_key)

    acme_connector = auth.connectors.connector if auth.connectors else make_connector(acme)
    acme_document = make_document(acme_connector, name="acme-handbook.md")
    globex_connector = make_connector(globex, name="Globex docs")
    database.add_connector(globex_connector)
    globex_document = make_document(globex_connector, name="globex-handbook.md")
    for document in (acme_document, globex_document):
        database.add_document(document)

    # Both called "alice" on purpose: the same external id in two organizations is the
    # ordinary case, and a lookup that matched on it alone would find the wrong person.
    acme_end_user = make_end_user(acme, external_id="alice")
    globex_end_user = make_end_user(globex, external_id="alice")
    for end_user in (acme_end_user, globex_end_user):
        database.add_end_user(end_user)
    acme_fact = make_fact(acme_end_user, text="Works on the Acme billing team.")
    globex_fact = make_fact(globex_end_user, text="Works on the Globex billing team.")
    for fact in (acme_fact, globex_fact):
        database.add_fact(fact)

    globex_evaluation_set = make_evaluation_set(globex_gateway)
    globex_evaluation_item = make_evaluation_item(globex_evaluation_set)
    globex_evaluation_run = make_evaluation_run(globex_evaluation_set)
    database.evaluation_sets[globex_evaluation_set.id] = globex_evaluation_set
    database.evaluation_items[globex_evaluation_item.id] = globex_evaluation_item
    database.evaluation_runs[globex_evaluation_run.id] = globex_evaluation_run
    # Task 104: a finished reprocessing run of Globex's connector, for the net.
    globex_reprocessing_run = make_reprocessing_run(globex_connector)
    database.reprocessing_runs[globex_reprocessing_run.id] = globex_reprocessing_run

    acme_log = make_log_row(acme, gateway_id=acme_gateway.id, api_key_id=acme_key.id)
    globex_log = make_log_row(globex, gateway_id=globex_gateway.id, api_key_id=globex_key.id)
    for row in (acme_log, globex_log):
        database.request_logs[row.id] = row

    platform = build_platform_fixture(database, settings=settings)

    return World(
        settings=settings,
        hasher=hasher,
        database=database,
        # The same instance the app's dependency override hands out, so a test cannot
        # accidentally exercise two services over one database.
        directory=auth.directory,
        catalog=auth.catalog,
        gateways=auth.gateways,
        probe=auth.probe,
        gateway_probe=auth.gateway_probe,
        cache=auth.cache,
        auth=auth,
        platform=platform,
        acme=acme,
        globex=globex,
        superadmin=people["superadmin"],
        acme_admin=people["acme_admin"],
        acme_member=people["acme_member"],
        acme_viewer=people["acme_viewer"],
        globex_admin=people["globex_admin"],
        acme_model=acme_model,
        globex_model=globex_model,
        global_model=global_model,
        acme_gateway=acme_gateway,
        globex_gateway=globex_gateway,
        acme_key=acme_key,
        globex_key=globex_key,
        acme_connector=acme_connector,
        globex_connector=globex_connector,
        acme_document=acme_document,
        globex_document=globex_document,
        acme_log=acme_log,
        globex_log=globex_log,
        acme_end_user=acme_end_user,
        globex_end_user=globex_end_user,
        acme_fact=acme_fact,
        globex_fact=globex_fact,
        globex_evaluation_set=globex_evaluation_set,
        globex_evaluation_item=globex_evaluation_item,
        globex_evaluation_run=globex_evaluation_run,
        globex_reprocessing_run=globex_reprocessing_run,
        people=people,
    )


__all__ = ["PASSWORD", "World", "build_world"]
