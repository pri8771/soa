"""Worker-side export delivery handler (EXP-008/010).

The API enqueues an ``export.deliver`` queue job at approval time for every
deliverable integration; this handler is what finally *claims and delivers*
one. :func:`~soa_worker.export_orchestrator.execute_export` owns the export
job's state machine (delivered -> SUCCEEDED, transient -> FAILED_RETRYABLE,
permanent -> FAILED_TERMINAL) and records each attempt. This handler binds
the tenant for RLS, injects the process-lifetime delivery dependencies (the
HTTP client, the SSRF egress allowlist, the secret store), stamps the
attempt time, and returns.

It mirrors how the stage handler manages domain failures internally rather
than raising to the queue: the queue job succeeds once the single attempt is
made, whatever the delivery outcome. A FAILED_RETRYABLE export is re-attempted
by re-enqueueing a fresh delivery job (the exports replay endpoint), not by the
queue job's own retry — so one queue job is always exactly one attempt.
"""

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from soa_config import SecretStore
from soa_db import DatabaseSessions
from soa_db.repository import OrganizationContext
from soa_db.tenant_guard import bind_tenant
from soa_storage.store import ObjectStore
from soa_worker.export_orchestrator import execute_export

__all__ = ["ExportDeliveryHandler"]


class ExportDeliveryHandler:
    """Runs one ``export.deliver`` queue job = one delivery attempt."""

    def __init__(
        self,
        db: DatabaseSessions,
        store: ObjectStore,
        client: httpx.AsyncClient,
        secret_store: SecretStore,
        allowlist: Sequence[str],
        *,
        resolve: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._db = db
        self._store = store
        self._client = client
        self._secret_store = secret_store
        self._allowlist = tuple(allowlist)
        # None (production) uses the SSRF-guarded default resolver that
        # refuses non-global addresses; tests inject a resolver.
        self._resolve = resolve

    async def handle(self, payload: Mapping[str, Any]) -> None:
        organization_id = uuid.UUID(str(payload["organization_id"]))
        export_job_id = uuid.UUID(str(payload["export_job_id"]))
        context = OrganizationContext(organization_id=organization_id)
        async with self._db.session_scope() as session:
            await bind_tenant(session, organization_id)
            await execute_export(
                session,
                self._store,
                context,
                export_job_id=export_job_id,
                client=self._client,
                allowlist=self._allowlist,
                timestamp=int(time.time()),
                secret_store=self._secret_store,
                resolve=self._resolve,
            )
