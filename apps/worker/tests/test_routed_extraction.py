"""Executable provider-chain routing, fallback, and durable telemetry."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy import select

from soa_db import Base, DatabaseSessions, create_database_engine
from soa_db.audit import AuditEvent
from soa_db.provider_metrics import ProviderRuntimeMetricRepository
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun, StageRun
from soa_worker.extraction.provider import (
    ExtractedField,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
    FieldSpec,
    PageInput,
)
from soa_worker.model_extraction_result import ModelOutputInvalidError
from soa_worker.model_usage import ProviderUsage
from soa_worker.provider_router import RoutingPolicy
from soa_worker.providers import (
    ANY_LANGUAGE,
    LOCAL_DATA_POLICY,
    Capability,
    ProviderInfo,
    register_provider,
    unregister_provider,
)
from soa_worker.routed_extraction import (
    ConfiguredExtractionProvider,
    RoutedExtractionProvider,
)

PRIMARY = "routing-test-primary"
FALLBACK = "routing-test-fallback"
ORG = OrganizationContext(uuid.UUID("11111111-1111-4111-8111-111111111111"))


class StubProvider:
    def __init__(
        self,
        name: str,
        extract: Callable[[ExtractionRequest], Awaitable[ExtractionResult]],
    ) -> None:
        self._name = name
        self._extract = extract
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def runtime_provenance(self) -> dict[str, object]:
        return {
            "adapter": f"stub:{self.name}",
            "model": f"model:{self.name}",
            "timeout_seconds": 10.0,
            "max_tokens": 500,
            "temperature": 0,
            "api_key": "must-never-persist",
        }

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        self.calls += 1
        return await self._extract(request)


@pytest.fixture(autouse=True)
def registered_chain() -> None:
    for name in (PRIMARY, FALLBACK):
        register_provider(
            ProviderInfo(
                name=name,
                capability=Capability.FIELD_EXTRACTION,
                languages=(ANY_LANGUAGE,),
                data_policy=LOCAL_DATA_POLICY,
            ),
            lambda: None,
        )
    yield
    for name in (PRIMARY, FALLBACK):
        unregister_provider(Capability.FIELD_EXTRACTION, name)


@pytest.fixture
def extraction_request() -> ExtractionRequest:
    return ExtractionRequest(
        document_id=uuid.uuid4(),
        document_sha256="a" * 64,
        content_type="application/pdf",
        pages=(PageInput(page_number=1, width_px=100, height_px=100, text="PO 123"),),
        fields=(FieldSpec(key="po_number", field_type="text"),),
    )


def result(provider: str, *, cost_cents: int = 0) -> ExtractionResult:
    return ExtractionResult(
        provider=provider,
        fields=(ExtractedField(field_key="po_number", raw_value="123", confidence=0.9),),
        cost_cents=cost_cents,
    )


def routed(
    primary: StubProvider,
    fallback: StubProvider,
    *,
    budget_cents: int | None = None,
    min_quality: float = 0,
) -> RoutedExtractionProvider:
    return RoutedExtractionProvider(
        (
            ConfiguredExtractionProvider(primary, estimated_cost_cents=3),
            ConfiguredExtractionProvider(fallback, estimated_cost_cents=2),
        ),
        policy=RoutingPolicy(
            allowed_providers=(PRIMARY, FALLBACK),
            preferred_order=(PRIMARY, FALLBACK),
            budget_cents=budget_cents,
            min_quality=min_quality,
        ),
        language="en",
    )


async def test_retryable_failure_uses_the_pinned_fallback_and_keeps_paid_cost(
    extraction_request: ExtractionRequest,
) -> None:
    async def fail(_request: ExtractionRequest) -> ExtractionResult:
        raise ModelOutputInvalidError(
            "invalid_json",
            cost_cents=4,
            usage=ProviderUsage(100, 20, 120),
            pricing_reference="primary-rate:v1",
            provider=PRIMARY,
            model="primary-model",
        )

    async def succeed(_request: ExtractionRequest) -> ExtractionResult:
        return result(FALLBACK, cost_cents=2)

    primary = StubProvider(PRIMARY, fail)
    fallback = StubProvider(FALLBACK, succeed)
    extracted = await routed(primary, fallback).extract(extraction_request)
    assert extracted.provider == FALLBACK
    assert extracted.cost_cents == 6
    assert primary.calls == fallback.calls == 1
    assert any("falling back" in warning for warning in extracted.warnings)
    assert any("included in the stage total" in warning for warning in extracted.warnings)
    assert [
        (item.provider, item.estimated_cost_cents, item.outcome) for item in extracted.usage_records
    ] == [
        (PRIMARY, 4, "invalid_output"),
        (FALLBACK, 2, "succeeded"),
    ]
    assert extracted.usage_records[0].usage == ProviderUsage(100, 20, 120)


async def test_terminal_failure_does_not_cross_the_fallback_boundary(
    extraction_request: ExtractionRequest,
) -> None:
    async def fail(_request: ExtractionRequest) -> ExtractionResult:
        raise ExtractionProviderError("invalid credential", retryable=False)

    async def succeed(_request: ExtractionRequest) -> ExtractionResult:
        return result(FALLBACK)

    primary = StubProvider(PRIMARY, fail)
    fallback = StubProvider(FALLBACK, succeed)
    with pytest.raises(ExtractionProviderError, match="invalid credential"):
        await routed(primary, fallback).extract(extraction_request)
    assert primary.calls == 1
    assert fallback.calls == 0


async def test_cumulative_estimates_block_an_over_budget_fallback(
    extraction_request: ExtractionRequest,
) -> None:
    async def fail(_request: ExtractionRequest) -> ExtractionResult:
        raise ExtractionProviderError("temporary provider outage", retryable=True)

    async def succeed(_request: ExtractionRequest) -> ExtractionResult:
        return result(FALLBACK)

    primary = StubProvider(PRIMARY, fail)
    fallback = StubProvider(FALLBACK, succeed)
    with pytest.raises(ExtractionProviderError, match="temporary provider outage"):
        await routed(primary, fallback, budget_cents=4).extract(extraction_request)
    assert primary.calls == 1
    assert fallback.calls == 0


async def test_unknown_quality_fails_closed_when_a_floor_is_pinned(
    extraction_request: ExtractionRequest,
) -> None:
    async def succeed(_request: ExtractionRequest) -> ExtractionResult:
        return result(PRIMARY)

    primary = StubProvider(PRIMARY, succeed)
    fallback = StubProvider(FALLBACK, succeed)
    with pytest.raises(ExtractionProviderError, match="routing refused") as caught:
        await routed(primary, fallback, min_quality=0.8).extract(extraction_request)
    assert caught.value.retryable is False
    assert primary.calls == fallback.calls == 0


async def test_runtime_context_records_route_audit_and_attempt_health(
    tmp_path: Path,
    extraction_request: ExtractionRequest,
) -> None:
    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/routing.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)
    committed_call_event_counts: list[int] = []

    async def observe_committed_call_evidence() -> None:
        # A separate connection can see the call-start row only after the
        # routing session committed it. This code runs inside the fake
        # provider invocation, before that invocation returns an outcome.
        async with db.session_scope() as observer:
            events = (
                (
                    await observer.execute(
                        select(AuditEvent.id).where(AuditEvent.action == "provider.call_started")
                    )
                )
                .scalars()
                .all()
            )
            committed_call_event_counts.append(len(events))

    async def fail(_request: ExtractionRequest) -> ExtractionResult:
        await observe_committed_call_evidence()
        raise ExtractionProviderError("temporary provider outage", retryable=True)

    async def succeed(_request: ExtractionRequest) -> ExtractionResult:
        await observe_committed_call_evidence()
        return result(FALLBACK, cost_cents=2)

    provider = routed(StubProvider(PRIMARY, fail), StubProvider(FALLBACK, succeed))
    run = ProcessingRun(
        id=uuid.uuid4(),
        organization_id=ORG.organization_id,
        document_id=uuid.uuid4(),
        run_number=1,
        input_sha256="a" * 64,
        triggered_by="test",
        provider_policy_version_id=uuid.uuid4(),
    )
    stage = StageRun(
        id=uuid.uuid4(),
        organization_id=ORG.organization_id,
        run_id=run.id,
        stage="extracting",
        attempt=1,
    )
    async with db.session_scope() as session:
        with provider.bind_execution(session, ORG, run, stage):
            extracted = await provider.extract(extraction_request)
            assert provider.runtime_provenance["selected_adapter"] == {
                "adapter": f"stub:{FALLBACK}",
                "model": f"model:{FALLBACK}",
                "timeout_seconds": 10.0,
                "max_tokens": 500,
                "temperature": 0,
            }
            assert provider.runtime_provenance["selected_provider"] == FALLBACK
            assert [attempt["provider"] for attempt in provider.runtime_provenance["attempts"]] == [
                PRIMARY,
                FALLBACK,
            ]
            assert [
                attempt["runtime_provenance"]["adapter"]
                for attempt in provider.runtime_provenance["attempts"]
            ] == [f"stub:{PRIMARY}", f"stub:{FALLBACK}"]
        assert "selected_adapter" not in provider.runtime_provenance
        assert extracted.provider == FALLBACK
        assert committed_call_event_counts == [1, 2]
        assert [
            attempt["provider"] for attempt in stage.output_summary["provider_call_attempts"]
        ] == [PRIMARY, FALLBACK]
        metrics = await ProviderRuntimeMetricRepository(session, ORG).list_for_capability(
            "field_extraction"
        )
        by_name = {metric.provider: metric for metric in metrics}
        assert by_name[PRIMARY].failure_count == 1
        assert by_name[FALLBACK].success_count == 1
        assert by_name[FALLBACK].fallback_count == 1
        audit_events = (await session.execute(select(AuditEvent))).scalars().all()
        actions = {event.action for event in audit_events}
        assert {
            "provider.route_selected",
            "provider.call_started",
            "provider.fallback_triggered",
        } <= actions
        call_events = [event for event in audit_events if event.action == "provider.call_started"]
        assert len(call_events) == 2
        call_summaries = {
            str(event.summary["provider"]): event.summary
            for event in call_events
            if event.summary is not None
        }
        assert set(call_summaries) == {PRIMARY, FALLBACK}
        assert call_summaries[PRIMARY]["fallback"] is False
        assert call_summaries[FALLBACK]["fallback"] is True
        assert all(
            len(str(summary["runtime_fingerprint"])) == 64 for summary in call_summaries.values()
        )
        assert all(
            summary["runtime_provenance"]["max_output_units"] == 500
            for summary in call_summaries.values()
        )
        assert all("must-never-persist" not in str(summary) for summary in call_summaries.values())
    await db.dispose()


async def test_repair_attempts_are_bounded_by_the_remaining_document_budget(
    extraction_request: ExtractionRequest,
) -> None:
    class RepairingProvider:
        name = PRIMARY
        supports_repair = True

        def __init__(self) -> None:
            self.calls = 0

        async def extract(
            self,
            _request: ExtractionRequest,
            *,
            repair_hint: str | None = None,
        ) -> ExtractionResult:
            self.calls += 1
            raise ModelOutputInvalidError(
                "invalid_json",
                cost_cents=3,
                usage=ProviderUsage(100, 10, 110),
                pricing_reference="primary-rate:v1",
                provider=PRIMARY,
                model="primary-model",
            )

    async def succeed(_request: ExtractionRequest) -> ExtractionResult:
        return result(FALLBACK, cost_cents=2)

    primary = RepairingProvider()
    fallback = StubProvider(FALLBACK, succeed)
    provider = RoutedExtractionProvider(
        (
            ConfiguredExtractionProvider(primary, estimated_cost_cents=3),
            ConfiguredExtractionProvider(fallback, estimated_cost_cents=2),
        ),
        policy=RoutingPolicy(
            allowed_providers=(PRIMARY, FALLBACK),
            preferred_order=(PRIMARY, FALLBACK),
            budget_cents=4,
        ),
        language="en",
    )
    extracted = await provider.extract(extraction_request)
    # One estimated 3-cent call fits. A second repair would not, so the
    # bounded repair returns honest absent values without overspending.
    assert primary.calls == 1
    assert fallback.calls == 0
    assert extracted.cost_cents == 3
    assert [item.outcome for item in extracted.usage_records] == ["invalid_output"]
    assert extracted.usage_records[0].usage == ProviderUsage(100, 10, 110)
    assert any("manual review" in warning for warning in extracted.warnings)


def test_mock_primary_with_real_fallback_never_skips_text_recognition() -> None:
    async def succeed(_request: ExtractionRequest) -> ExtractionResult:
        return result(PRIMARY)

    provider = RoutedExtractionProvider(
        (
            ConfiguredExtractionProvider(StubProvider("mock", succeed)),
            ConfiguredExtractionProvider(StubProvider(PRIMARY, succeed)),
        ),
        policy=RoutingPolicy(allowed_providers=("mock", PRIMARY)),
        language="en",
    )
    assert provider.name == "routed-extraction"


async def test_health_reroute_is_counted_as_a_fallback(
    tmp_path: Path,
    extraction_request: ExtractionRequest,
) -> None:
    from soa_db.provider_metrics import record_provider_attempt

    engine = create_database_engine(f"sqlite+aiosqlite:///{tmp_path}/health-fallback.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    db = DatabaseSessions(engine)

    async def succeed(_request: ExtractionRequest) -> ExtractionResult:
        return result(FALLBACK)

    provider = routed(StubProvider(PRIMARY, succeed), StubProvider(FALLBACK, succeed))
    run = ProcessingRun(
        id=uuid.uuid4(),
        organization_id=ORG.organization_id,
        document_id=uuid.uuid4(),
        run_number=1,
        input_sha256="a" * 64,
        triggered_by="test",
        provider_policy_version_id=uuid.uuid4(),
    )
    stage = StageRun(
        id=uuid.uuid4(),
        organization_id=ORG.organization_id,
        run_id=run.id,
        stage="extracting",
        attempt=1,
    )
    async with db.session_scope() as session:
        for _ in range(3):
            await record_provider_attempt(
                session,
                ORG,
                capability="field_extraction",
                provider=PRIMARY,
                succeeded=False,
                fallback=False,
                latency_ms=1,
                failure_class="retryable",
            )
        with provider.bind_execution(session, ORG, run, stage):
            extracted = await provider.extract(extraction_request)
        assert extracted.provider == FALLBACK
        metric = await ProviderRuntimeMetricRepository(session, ORG).get_for_provider(
            "field_extraction", FALLBACK
        )
        assert metric is not None and metric.fallback_count == 1
    await db.dispose()
