"""Resilience tests (REL-010).

Each failure mode the platform must survive, exercised against the REAL
components, asserting the two invariants that matter: **no data loss or
duplicate business delivery**, and **user-visible state stays
recoverable**. One class per mode:

- worker kill → the crashed worker's job returns to the queue (JOB-004);
- provider timeout/outage → classified retryable, the document is not
  lost (PRC-004 / JOB-005);
- database transient error → the job reschedules with backoff, a
  permanent error dead-letters instead of looping (JOB-005);
- storage failure → missing / corrupted objects raise, never a silent
  success, so reconciliation can catch them (STO-005);
- duplicate event → idempotency collapses it to one effect (JOB-002 /
  EXP-008);
- receiver timeout → a failed delivery is retried, the export intent
  stays single, and delivery happens exactly once (EXP-005/008).
"""

import uuid
from datetime import timedelta

import pytest

from soa_db import Base, DatabaseSessions, create_database_engine, utcnow
from soa_db.exports import (
    DeliveryAttemptRepository,
    create_export_job,
    export_business_key,
    record_delivery_attempt,
)
from soa_db.jobs import (
    FailureClass,
    JobStatus,
    claim_next_jobs,
    enqueue_job,
    mark_failed,
    recover_expired_locks,
)
from soa_db.repository import OrganizationContext


@pytest.fixture
async def sessions(tmp_path: object) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/resilience.db")  # type: ignore[str-bytes-safe]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


class TestWorkerKill:
    async def test_crashed_worker_job_returns_to_the_queue(
        self, sessions: DatabaseSessions
    ) -> None:
        async with sessions.session_scope() as session:
            await enqueue_job(session, job_type="document.extract", payload={})

        # Worker A claims the job, then "dies" — no heartbeat, no completion.
        async with sessions.session_scope() as session:
            (claimed,) = await claim_next_jobs(session, worker_id="worker-A", limit=1)
            assert claimed.status == JobStatus.RUNNING
            job_id = claimed.id

        # After the lock expires, recovery returns it to pending — the work
        # is not stranded on a dead worker.
        async with sessions.session_scope() as session:
            recovered = await recover_expired_locks(session, now=utcnow() + timedelta(hours=1))
            assert [j.id for j in recovered] == [job_id]
            assert recovered[0].status == JobStatus.PENDING
            assert recovered[0].lock_owner is None

        # And a fresh worker can claim it again — no data loss.
        async with sessions.session_scope() as session:
            (reclaimed,) = await claim_next_jobs(session, worker_id="worker-B", limit=1)
            assert reclaimed.id == job_id
            assert reclaimed.attempts == 2  # attempt count preserved across the crash

    async def test_crash_on_final_attempt_dead_letters_not_loops(
        self, sessions: DatabaseSessions
    ) -> None:
        async with sessions.session_scope() as session:
            await enqueue_job(session, job_type="doomed", payload={}, max_attempts=1)
        async with sessions.session_scope() as session:
            await claim_next_jobs(session, worker_id="worker-A", limit=1)
        async with sessions.session_scope() as session:
            (recovered,) = await recover_expired_locks(session, now=utcnow() + timedelta(hours=1))
            # Its one attempt is spent, so it dead-letters instead of
            # resurrecting forever.
            assert recovered.status == JobStatus.DEAD_LETTER


class TestDatabaseTransientErrorRetry:
    async def test_transient_failure_reschedules_permanent_dead_letters(
        self, sessions: DatabaseSessions
    ) -> None:
        async with sessions.session_scope() as session:
            await enqueue_job(session, job_type="flaky", payload={}, max_attempts=5)
        async with sessions.session_scope() as session:
            (job,) = await claim_next_jobs(session, worker_id="w", limit=1)
            # A transient DB blip: rescheduled for a later retry, not lost.
            mark_failed(
                job,
                worker_id="w",
                error="database connection reset",
                failure_class=FailureClass.TRANSIENT,
            )
            assert job.status == JobStatus.PENDING
            assert job.run_after is not None and job.run_after > utcnow()

        async with sessions.session_scope() as session:
            await enqueue_job(session, job_type="poison", payload={}, max_attempts=5)
        async with sessions.session_scope() as session:
            (job,) = await claim_next_jobs(session, worker_id="w", job_types=["poison"], limit=1)
            # A permanent error (bad input) dead-letters immediately —
            # retrying it forever would just burn the queue.
            mark_failed(
                job,
                worker_id="w",
                error="schema violation",
                failure_class=FailureClass.PERMANENT,
            )
            assert job.status == JobStatus.DEAD_LETTER

    async def test_exhausted_transient_retries_dead_letter(
        self, sessions: DatabaseSessions
    ) -> None:
        async with sessions.session_scope() as session:
            await enqueue_job(session, job_type="always-blips", payload={}, max_attempts=1)
        async with sessions.session_scope() as session:
            (job,) = await claim_next_jobs(session, worker_id="w", limit=1)
            # Transient, but no attempts remain -> dead-letter, never a
            # silent drop.
            mark_failed(job, worker_id="w", error="timeout", failure_class=FailureClass.TRANSIENT)
            assert job.status == JobStatus.DEAD_LETTER


class TestDuplicateEvent:
    async def test_duplicate_enqueue_collapses_to_one_job(self, sessions: DatabaseSessions) -> None:
        async with sessions.session_scope() as session:
            first = await enqueue_job(
                session, job_type="document.preprocess", payload={}, dedupe_key="doc-42"
            )
            second = await enqueue_job(
                session, job_type="document.preprocess", payload={}, dedupe_key="doc-42"
            )
            # The same triggering event twice yields ONE job, not two —
            # duplicate delivery of the same document cannot fan out.
            assert first.id == second.id

        async with sessions.session_scope() as session:
            claimed = await claim_next_jobs(session, worker_id="w", limit=10)
            assert len(claimed) == 1


class TestProviderOutage:
    def test_timeout_is_retryable_bad_input_is_terminal(self) -> None:
        from soa_worker.rendering import RenderError, RenderFailure

        # A provider/renderer timeout says nothing about the document —
        # it must be retryable so the document is reprocessed, not lost.
        timeout = RenderError(RenderFailure.TIMEOUT, "rendering exceeded the budget")
        assert timeout.retryable is True
        crashed = RenderError(RenderFailure.CRASHED, "renderer exited 139")
        assert crashed.retryable is True

        # A malformed file or a limit breach IS about the document — retrying
        # would loop forever, so it is terminal.
        malformed = RenderError(RenderFailure.MALFORMED, "not a pdf")
        assert malformed.retryable is False
        over_limit = RenderError(RenderFailure.LIMIT_EXCEEDED, "too many pages")
        assert over_limit.retryable is False


class TestStorageFailure:
    async def test_missing_and_corrupted_objects_raise_not_swallow(self) -> None:
        from soa_storage import ChecksumMismatchError, MemoryObjectStore, ObjectNotFoundError

        store = MemoryObjectStore()
        # A read for an object that isn't there fails loudly — never a
        # silent empty success that would poison downstream stages.
        with pytest.raises(ObjectNotFoundError):
            await store.get("missing/key")

        # The integrity guard refuses to WRITE bytes that don't match the
        # declared checksum, so a corrupted upload never becomes a stored
        # object that later reads back as "valid" wrong bytes.
        with pytest.raises(ChecksumMismatchError):
            await store.put("k", b"the real bytes", sha256="00" * 32)

        # A correct write reads back intact (get re-verifies the stored
        # checksum), and a delete of a missing key is surfaced, not silent.
        await store.put("k", b"the real bytes")
        assert await store.get("k") == b"the real bytes"
        with pytest.raises(ObjectNotFoundError):
            await store.delete("never/written")


class TestReceiverTimeout:
    async def test_failed_delivery_retries_without_duplicate_business_delivery(
        self, sessions: DatabaseSessions
    ) -> None:
        org = OrganizationContext(organization_id=uuid.uuid4())
        ids = {
            name: uuid.uuid4() for name in ("document", "run", "canonical", "integration", "map")
        }
        key = export_business_key(ids["integration"], ids["document"], ids["run"])

        async with sessions.session_scope() as session:
            job = await create_export_job(
                session,
                org,
                document_id=ids["document"],
                run_id=ids["run"],
                canonical_payload_id=ids["canonical"],
                integration_id=ids["integration"],
                mapping_version_id=ids["map"],
                actor_id="user:integration-admin",
                business_key=key,
            )
            # A duplicate approval event for the same order returns the SAME
            # export intent — never a second delivery.
            duplicate = await create_export_job(
                session,
                org,
                document_id=ids["document"],
                run_id=ids["run"],
                canonical_payload_id=ids["canonical"],
                integration_id=ids["integration"],
                mapping_version_id=ids["map"],
                actor_id="user:integration-admin",
                business_key=key,
            )
            assert duplicate.id == job.id

            # The receiver times out on the first attempt (retryable), then
            # accepts on the second. Attempts are append-only evidence.
            await record_delivery_attempt(
                session, org, job=job, outcome="retryable_error", safe_error="receiver timeout"
            )
            await record_delivery_attempt(
                session, org, job=job, outcome="delivered", response_status=200
            )
            assert job.attempt_count == 2

        async with sessions.session_scope() as session:
            attempts = await DeliveryAttemptRepository(session, org).list_for_job(job.id)
            delivered = [a for a in attempts if a.outcome == "delivered"]
            # Exactly one successful business delivery, despite the retry.
            assert len(delivered) == 1
            assert len(attempts) == 2
