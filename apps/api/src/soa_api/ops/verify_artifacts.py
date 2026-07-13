"""Artifact manifest verification command (STO-005).

Run ``uv run python -m soa_api.ops.verify_artifacts`` (or ``make
verify-artifacts``) with API storage/database settings in the
environment. Exports every organization's artifact manifest (binding
each tenant so RLS admits the reads), reconciles it against the object
store, and prints a JSON report of missing, extra, and hash-mismatched
objects — keys and hashes only, never content. Exit code 1 when the
store and the records disagree.
"""

import asyncio
import json
import sys

from sqlalchemy import select

from soa_api.domain.tenancy import Organization
from soa_api.settings import load_settings
from soa_db import DatabaseSessions, create_database_engine
from soa_db.artifacts import export_manifest
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_storage import ObjectStore
from soa_storage.manifest import ReconciliationReport, reconcile
from soa_storage.s3 import S3ObjectStore, S3Settings

#: All artifact keys live under this namespace (soa_storage.keys); extra
#: object detection is scoped to it so unrelated bucket contents (if the
#: bucket is shared, which it shouldn't be) don't flood the report.
ARTIFACT_PREFIX = "orgs/"


async def collect_expected_manifest(db: DatabaseSessions) -> dict[str, str]:
    """Merge every organization's manifest. Object keys are globally
    unique, so merging cannot collide."""
    async with db.session_scope() as session:
        organization_ids = list((await session.execute(select(Organization.id))).scalars().all())
    expected: dict[str, str] = {}
    for organization_id in organization_ids:
        async with db.session_scope() as session:
            await bind_tenant(session, organization_id)
            manifest = await export_manifest(
                session, OrganizationContext(organization_id=organization_id)
            )
            expected.update(manifest)
    return expected


async def verify_artifacts(db: DatabaseSessions, store: ObjectStore) -> ReconciliationReport:
    expected = await collect_expected_manifest(db)
    return await reconcile(store, expected, prefix=ARTIFACT_PREFIX)


async def _main() -> int:
    settings = load_settings()
    if not settings.storage_endpoint_url:
        print("storage_endpoint_url is not configured; nothing to verify", file=sys.stderr)
        return 2
    store = S3ObjectStore(
        S3Settings(
            endpoint_url=settings.storage_endpoint_url,
            access_key=settings.storage_access_key or "",
            secret_key=settings.storage_secret_key or "",
            bucket=settings.storage_bucket,
            region=settings.storage_region,
            force_path_style=settings.storage_force_path_style,
            sse=settings.storage_sse,
            sse_kms_key_id=settings.storage_sse_kms_key_id,
        )
    )
    db = DatabaseSessions(create_database_engine(settings.database_url))
    try:
        report = await verify_artifacts(db, store)
    finally:
        await db.dispose()
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
