"""Immutable per-run configuration verification."""

import uuid
from pathlib import Path

import pytest

from soa_api.domain.policies import PolicyVersion
from soa_api.domain.rules import RuleSetVersion
from soa_api.domain.schemas import SchemaVersion
from soa_api.domain.streams import StreamVersion
from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.instructions import InstructionVersion
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun
from soa_worker.run_config import (
    RunConfigError,
    execution_fingerprint,
    load_resolved_run_config,
    snapshot_fingerprint,
    verify_run_config,
)

ORG = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-4222-8222-222222222222")
VERSION = uuid.UUID("33333333-3333-4333-8333-333333333333")
SCHEMA = uuid.UUID("44444444-4444-4444-8444-444444444444")
RULES = uuid.UUID("55555555-5555-4555-8555-555555555555")
POLICY = uuid.UUID("66666666-6666-4666-8666-666666666666")
PROCESS = uuid.UUID("77777777-7777-4777-8777-777777777777")
CONFIDENCE = uuid.UUID("88888888-8888-4888-8888-888888888888")
INSTRUCTION = uuid.UUID("99999999-9999-4999-8999-999999999999")


@pytest.fixture
async def db(tmp_path: Path) -> DatabaseSessions:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/config.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return DatabaseSessions(engine)


def _snapshot() -> dict[str, object]:
    value: dict[str, object] = {
        "process_version_id": str(uuid.uuid4()),
        "process_version_number": 4,
        "config": {"languages": ["en"]},
    }
    value["fingerprint"] = snapshot_fingerprint(value)
    return value


def _run(fingerprint: str) -> ProcessingRun:
    run = ProcessingRun(
        organization_id=ORG,
        document_id=uuid.uuid4(),
        run_number=1,
        stream_version_id=VERSION,
        config_fingerprint=fingerprint,
        provider_policy_version_id=POLICY,
        input_sha256="a" * 64,
        triggered_by="test",
    )
    run.execution_fingerprint = execution_fingerprint(run)
    return run


async def _seed(db: DatabaseSessions, snapshot: dict[str, object]) -> None:
    async with db.session_scope() as session:
        session.add(
            StreamVersion(
                id=VERSION,
                organization_id=ORG,
                stream_id=uuid.uuid4(),
                version_number=1,
                overrides={},
                resolved_snapshot=snapshot,
                state="published",
            )
        )


async def test_verifies_exact_tenant_version_and_fingerprint(db: DatabaseSessions) -> None:
    snapshot = _snapshot()
    await _seed(db, snapshot)
    async with db.session_scope() as session:
        resolved = await verify_run_config(
            session,
            OrganizationContext(organization_id=ORG),
            _run(str(snapshot["fingerprint"])),
        )
    assert resolved == snapshot


async def test_rejects_run_fingerprint_mismatch(db: DatabaseSessions) -> None:
    await _seed(db, _snapshot())
    async with db.session_scope() as session:
        with pytest.raises(RunConfigError, match="does not match"):
            await verify_run_config(
                session,
                OrganizationContext(organization_id=ORG),
                _run("f" * 64),
            )


async def test_rejects_cross_tenant_lookup(db: DatabaseSessions) -> None:
    snapshot = _snapshot()
    await _seed(db, snapshot)
    async with db.session_scope() as session:
        with pytest.raises(RunConfigError, match="does not exist for this tenant"):
            await verify_run_config(
                session,
                OrganizationContext(organization_id=OTHER_ORG),
                _run(str(snapshot["fingerprint"])),
            )


async def test_resolves_pinned_schema_rules_languages_and_provider(db: DatabaseSessions) -> None:
    snapshot: dict[str, object] = {
        "process_version_id": str(uuid.uuid4()),
        "process_version_number": 2,
        "config": {
            "schema_version_id": str(SCHEMA),
            "rule_set_version_id": str(RULES),
            "provider_policy_version_id": str(POLICY),
            "confidence_policy_version_id": str(CONFIDENCE),
            "languages": ["EN", "es"],
            "locale": "en-GB",
            "currency": "GBP",
        },
    }
    snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    await _seed(db, snapshot)
    async with db.session_scope() as session:
        session.add_all(
            [
                SchemaVersion(
                    id=SCHEMA,
                    organization_id=ORG,
                    process_id=PROCESS,
                    version_number=3,
                    state="superseded",
                    definition={
                        "fields": [
                            {
                                "key": "po_number",
                                "label": "PO number",
                                "type": "text",
                                "criticality": "critical",
                                "normalization": "identifier",
                            },
                            {
                                "key": "lines",
                                "label": "Lines",
                                "type": "table",
                                "columns": [
                                    {
                                        "key": "sku",
                                        "label": "SKU",
                                        "type": "text",
                                    }
                                ],
                            },
                        ]
                    },
                ),
                RuleSetVersion(
                    id=RULES,
                    organization_id=ORG,
                    process_id=PROCESS,
                    version_number=5,
                    state="published",
                    definition={
                        "version": "custom-5",
                        "rules": [
                            {
                                "key": "required.po_number",
                                "severity": "error",
                                "action": "block",
                                "condition": {
                                    "op": "not",
                                    "arg": {"op": "is_present", "key": "po_number"},
                                },
                            }
                        ],
                    },
                ),
                PolicyVersion(
                    id=POLICY,
                    organization_id=ORG,
                    policy_type="provider",
                    version_number=1,
                    state="published",
                    definition={
                        "provider_name": "mock",
                        "capabilities": ["ocr", "field_extraction"],
                    },
                ),
                PolicyVersion(
                    id=CONFIDENCE,
                    organization_id=ORG,
                    policy_type="confidence",
                    version_number=2,
                    state="superseded",
                    definition={
                        "floor": 0.9,
                        "critical_floor": 0.995,
                        "field_overrides": {"lines.sku": 0.97},
                    },
                ),
                InstructionVersion(
                    id=INSTRUCTION,
                    organization_id=ORG,
                    stream_version_id=VERSION,
                    schema_version_id=SCHEMA,
                    version_number=3,
                    state="superseded",
                    content={
                        "instructions": "Use visible document values only.",
                        "field_guidance": {"po_number": "Preserve zeroes."},
                    },
                ),
            ]
        )

    run = _run(str(snapshot["fingerprint"]))
    run.confidence_policy_version_id = CONFIDENCE
    run.instruction_version_id = INSTRUCTION
    run.execution_fingerprint = execution_fingerprint(run)
    async with db.session_scope() as session:
        resolved = await load_resolved_run_config(
            session,
            OrganizationContext(organization_id=ORG),
            run,
        )

    assert resolved.provider_name == "mock"
    assert resolved.pipeline.rules_version == "custom-5"
    assert resolved.pipeline.languages == ("en", "es")
    assert resolved.pipeline.normalization.locale == "en-GB"
    assert resolved.pipeline.normalization.currency == "GBP"
    assert [field.key for field in resolved.pipeline.field_specs] == [
        "po_number",
        "lines",
        "lines.sku",
    ]
    assert resolved.pipeline.criticality["po_number"] == "critical"
    assert resolved.pipeline.normalizer_overrides["po_number"] == "identifier"
    assert resolved.pipeline.confidence_policy.critical_min_confidence == 0.995
    assert resolved.pipeline.confidence_policy.field_min_confidence["lines.sku"] == 0.97
    assert resolved.instruction_reference == f"instruction:{INSTRUCTION}:v3"
    assert resolved.instructions is not None
