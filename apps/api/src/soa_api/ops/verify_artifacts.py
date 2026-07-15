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
from soa_api.settings import ApiSettings, load_settings
from soa_db import DatabaseSessions, create_database_engine
from soa_db.artifacts import export_manifest
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_storage import ObjectStore
from soa_storage.filesystem import FilesystemObjectStore
from soa_storage.gcs import GcsObjectStore, GcsSettings
from soa_storage.manifest import ReconciliationReport, reconcile
from soa_storage.s3 import S3ObjectStore, S3Settings

#: All artifact keys live under this namespace (soa_storage.keys); extra
#: object detection is scoped to it so unrelated bucket contents (if the
#: bucket is shared, which it shouldn't be) don't flood the report.
ARTIFACT_PREFIX = "orgs/"


def build_object_store(settings: ApiSettings) -> ObjectStore:
    """Build the configured store without assuming an S3 endpoint.

    The deployment uses GCS, while development may use filesystem or S3.
    Fail before touching the database when a selected backend is incomplete;
    a verifier that silently skips the production backend is worse than no
    verifier because it creates false recovery confidence.
    """
    if settings.storage_backend == "filesystem":
        return FilesystemObjectStore(
            root=settings.storage_filesystem_root,
            base_url=settings.storage_local_base_url,
        )
    if settings.storage_backend == "gcs":
        if not settings.storage_gcs_project:
            raise ValueError("GCS artifact verification requires storage_gcs_project")
        return GcsObjectStore(
            GcsSettings(
                bucket=settings.storage_bucket,
                project=settings.storage_gcs_project,
                kms_key_name=settings.storage_gcs_kms_key_name,
            )
        )
    if not (
        settings.storage_endpoint_url
        and settings.storage_access_key
        and settings.storage_secret_key
    ):
        raise ValueError("S3 artifact verification requires endpoint, access key, and secret key")
    return S3ObjectStore(
        S3Settings(
            endpoint_url=settings.storage_endpoint_url,
            access_key=settings.storage_access_key,
            secret_key=settings.storage_secret_key,
            bucket=settings.storage_bucket,
            region=settings.storage_region,
            force_path_style=settings.storage_force_path_style,
            sse=settings.storage_sse,
            sse_kms_key_id=settings.storage_sse_kms_key_id,
        )
    )


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
    try:
        store = build_object_store(settings)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    db = DatabaseSessions(create_database_engine(settings.database_url))
    try:
        report = await verify_artifacts(db, store)
    finally:
        await db.dispose()
    print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
