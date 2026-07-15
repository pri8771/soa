"""Seed a ready-to-use demo tenant for local development.

Creates (idempotently) the Northstar demo organization with the dev admin
as owner, a published purchase-order process, and an active "uploads"
stream — so a fresh local database has somewhere to upload a PO and the
dev login lands on a populated tenant.

Run via ``make seed`` (which points it at the configured database). Refuses
to run against a production configuration.
"""

import asyncio

from soa_api.auth.dev_identity import authenticate_dev_user
from soa_api.domain.processes import (
    ProcessRepository,
    ProcessVersionRepository,
    create_draft,
    create_process,
    publish_draft,
)
from soa_api.domain.streams import (
    StreamRepository,
    create_stream,
    create_stream_draft,
    publish_stream_draft,
)
from soa_api.domain.tenancy import OrganizationRepository
from soa_api.services import tenancy_service
from soa_api.settings import ApiSettings
from soa_db import DatabaseSessions, create_database_engine
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant

DEMO_ORG_NAME = "Northstar Trading"
DEMO_ORG_SLUG = "northstar"
DEMO_ADMIN = "admin@northstar.example"
PROCESS_SLUG = "purchase-orders"
STREAM_SLUG = "uploads"
ACTOR = "system:seed"
PROCESS_DEFINITION = {
    "language": "en",
    "confidence_floor": 0.8,
    "provider": "default-ocr",
    # Local demos re-upload the same sample PO constantly — process the
    # repeats (ING-006 "allow": still marked and audited, never reviewed
    # merely for being a duplicate). Inherited by the demo stream.
    "duplicate_policy": "allow",
}


async def _seed(db: DatabaseSessions) -> None:
    principal = authenticate_dev_user(DEMO_ADMIN)
    async with db.session_scope() as session:
        org_repo = OrganizationRepository(session)
        organization = await org_repo.get_by_slug(DEMO_ORG_SLUG)
        if organization is None:
            created = await tenancy_service.create_organization(
                session, principal, name=DEMO_ORG_NAME, slug=DEMO_ORG_SLUG
            )
            organization = created.organization
            print(f"  created organization {DEMO_ORG_SLUG!r}")
        else:
            print(f"  organization {DEMO_ORG_SLUG!r} already exists")

        context = OrganizationContext(organization_id=organization.id)
        await bind_tenant(session, organization.id)

        process = await ProcessRepository(session, context).get_by_slug(PROCESS_SLUG)
        if process is None:
            process = await create_process(
                session, context, name="Purchase Orders", slug=PROCESS_SLUG, actor_id=ACTOR
            )
            draft = await create_draft(
                session,
                context,
                process=process,
                definition=dict(PROCESS_DEFINITION),
                actor_id=ACTOR,
            )
            await publish_draft(session, context, process=process, draft=draft, actor_id=ACTOR)
            process_version = draft
            print(f"  published process {PROCESS_SLUG!r}")
        else:
            process_version = await ProcessVersionRepository(session, context).get_published(
                process.id
            )
            print(f"  process {PROCESS_SLUG!r} already exists")
        assert process_version is not None, "process has no published version"

        stream = await StreamRepository(session, context).get_by_slug(STREAM_SLUG)
        if stream is None:
            stream = await create_stream(
                session,
                context,
                process_id=process.id,
                name="Purchase order uploads",
                slug=STREAM_SLUG,
                actor_id=ACTOR,
            )
            draft = await create_stream_draft(
                session, context, stream=stream, overrides={}, actor_id=ACTOR
            )
            await publish_stream_draft(
                session,
                context,
                stream=stream,
                draft=draft,
                process_version=process_version,
                actor_id=ACTOR,
            )
            print(f"  published active stream {STREAM_SLUG!r}")
        else:
            print(f"  stream {STREAM_SLUG!r} already exists")


async def _main() -> None:
    settings = ApiSettings()
    if settings.is_production:
        raise SystemExit("refusing to seed a production configuration")
    engine = create_database_engine(settings.database_url)
    db = DatabaseSessions(engine)
    print("Seeding demo tenant…")
    await _seed(db)
    print(
        f"Done. Sign in as {DEMO_ADMIN} and open "
        f"/app/{DEMO_ORG_SLUG}/documents/upload to process a PO."
    )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
