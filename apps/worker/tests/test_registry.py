import uuid

import pytest

from soa_db.uploads import UPLOAD_CLEANUP_JOB_TYPE
from soa_worker.main import (
    DATA_EXPORT_CLEANUP_JOB_TYPE,
    DATA_EXPORT_JOB_TYPE,
    DOCUMENT_DELETION_JOB_TYPE,
    EVALUATION_JOB_TYPE,
    EXPORT_JOB_TYPE,
    PREPROCESS_JOB_TYPE,
    STAGE_JOB_TYPE,
    register_tenant_handler,
)
from soa_worker.registry import HandlerRegistry, JobEnvelope

TENANT_JOB_TYPES = (
    PREPROCESS_JOB_TYPE,
    STAGE_JOB_TYPE,
    UPLOAD_CLEANUP_JOB_TYPE,
    EXPORT_JOB_TYPE,
    EVALUATION_JOB_TYPE,
    DATA_EXPORT_JOB_TYPE,
    DATA_EXPORT_CLEANUP_JOB_TYPE,
    DOCUMENT_DELETION_JOB_TYPE,
)


def test_register_and_resolve() -> None:
    registry = HandlerRegistry()

    @registry.register("document.process")
    async def handle(job: JobEnvelope) -> None:
        pass

    assert registry.resolve("document.process") is handle
    assert registry.registered_types == ["document.process"]


def test_duplicate_registration_is_rejected() -> None:
    registry = HandlerRegistry()

    @registry.register("document.process")
    async def first(job: JobEnvelope) -> None:
        pass

    with pytest.raises(ValueError, match="already registered"):

        @registry.register("document.process")
        async def second(job: JobEnvelope) -> None:
            pass


def test_resolving_unknown_type_raises() -> None:
    registry = HandlerRegistry()
    with pytest.raises(KeyError, match="no handler registered"):
        registry.resolve("unknown.type")


@pytest.mark.parametrize("job_type", TENANT_JOB_TYPES)
async def test_tenant_handler_fences_payload_against_queue_envelope(job_type: str) -> None:
    registry = HandlerRegistry()
    called: list[uuid.UUID] = []

    @register_tenant_handler(registry, job_type)
    async def handler(_job: JobEnvelope, organization_id: uuid.UUID) -> None:
        called.append(organization_id)

    expected = uuid.uuid4()
    resolved = registry.resolve(job_type)
    await resolved(
        JobEnvelope(
            job_type=job_type,
            organization_id=expected,
            payload={"organization_id": str(expected)},
        )
    )
    assert called == [expected]

    with pytest.raises(ValueError, match="does not match"):
        await resolved(
            JobEnvelope(
                job_type=job_type,
                organization_id=expected,
                payload={"organization_id": str(uuid.uuid4())},
            )
        )
    with pytest.raises(ValueError, match="trusted tenant"):
        await resolved(
            JobEnvelope(
                job_type=job_type,
                payload={"organization_id": str(expected)},
            )
        )
    with pytest.raises(ValueError, match="valid tenant payload"):
        await resolved(JobEnvelope(job_type=job_type, organization_id=expected, payload={}))
    assert called == [expected]
