"""Executable, policy-bound extraction routing and fallback.

``provider_router.route`` deliberately stays pure.  This adapter is the
runtime seam that turns its decision into bounded calls while preserving the
normal :class:`ExtractionProvider` contract consumed by the pipeline.

Only providers and secret references present in the immutable policy are
constructed.  Fallback occurs for retryable provider failures (and invalid
provider contract output), never for a tenant-policy violation or a terminal
request/configuration error.  Every decision and fallback is written to the
append-only audit trail and every attempt updates tenant-scoped aggregate
health metrics without storing document content or raw vendor errors.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from soa_db.audit import ActorType, record_audit_event
from soa_db.provider_metrics import provider_operational_signals, record_provider_attempt
from soa_db.repository import OrganizationContext
from soa_db.runs import ProcessingRun, StageRun
from soa_db.tenant_guard import bind_tenant
from soa_worker.extraction.provider import (
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
    validate_result_against_request,
)
from soa_worker.extraction_repair import (
    RepairableExtractionProvider,
    RepairPolicy,
    extract_with_repair,
)
from soa_worker.model_usage import ProviderCallUsage
from soa_worker.provider_router import (
    DocumentFacts,
    OperationalSignals,
    RoutingPolicy,
    route,
)
from soa_worker.providers import Capability

ACTOR = "system:provider-router"
MAX_RUNTIME_PROVIDER_CHAIN = 8


@dataclass(frozen=True)
class ConfiguredExtractionProvider:
    provider: ExtractionProvider
    estimated_cost_cents: int | None = None

    def __post_init__(self) -> None:
        if self.estimated_cost_cents is not None and self.estimated_cost_cents < 0:
            raise ValueError("provider cost estimate cannot be negative")


@dataclass(frozen=True)
class RoutingExecutionContext:
    session: AsyncSession
    organization: OrganizationContext
    run: ProcessingRun
    stage_run: StageRun


class RoutedExtractionProvider:
    """One extraction provider interface backed by an immutable chain."""

    def __init__(
        self,
        providers: tuple[ConfiguredExtractionProvider, ...],
        *,
        policy: RoutingPolicy,
        language: str | None,
    ) -> None:
        if not providers:
            raise ValueError("the extraction routing chain cannot be empty")
        if len(providers) > MAX_RUNTIME_PROVIDER_CHAIN:
            raise ValueError(
                f"the extraction routing chain cannot exceed {MAX_RUNTIME_PROVIDER_CHAIN}"
            )
        names = tuple(item.provider.name for item in providers)
        if len(set(names)) != len(names):
            raise ValueError("the extraction routing chain contains duplicate providers")
        if policy.allowed_providers is None or set(policy.allowed_providers) != set(names):
            raise ValueError("routing policy must pin exactly the constructed provider chain")
        self._providers = {item.provider.name: item for item in providers}
        self._policy = policy
        self._language = language
        self._execution: ContextVar[RoutingExecutionContext | None] = ContextVar(
            f"provider-routing-execution-{id(self)}", default=None
        )
        self._selected_provenance: ContextVar[dict[str, Any] | None] = ContextVar(
            f"provider-routing-provenance-{id(self)}", default=None
        )

    @property
    def name(self) -> str:
        # Preserve the deterministic mock's no-recognition test path only when
        # every possible adapter is the mock.  Any real fallback needs page
        # text prepared before extraction begins.
        names = tuple(self._providers)
        if names == ("mock",):
            return "mock"
        return names[0] if names[0] != "mock" else "routed-extraction"

    @property
    def runtime_provenance(self) -> dict[str, Any]:
        return {
            "router": "pinned-provider-chain-v1",
            **(self._selected_provenance.get() or {}),
        }

    @contextmanager
    def bind_execution(
        self,
        session: AsyncSession,
        organization: OrganizationContext,
        run: ProcessingRun,
        stage_run: StageRun,
    ) -> Iterator[None]:
        execution_token = self._execution.set(
            RoutingExecutionContext(
                session=session,
                organization=organization,
                run=run,
                stage_run=stage_run,
            )
        )
        provenance_token = self._selected_provenance.set(None)
        try:
            yield
        finally:
            # A credential-free routed adapter may be cached across runs. Keep
            # the selected provider identity scoped to this execution so a
            # later failed run cannot inherit stale provenance from an earlier
            # successful run in the same async task.
            self._selected_provenance.reset(provenance_token)
            self._execution.reset(execution_token)

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        execution = self._execution.get()
        health: dict[str, str] = {}
        quality: dict[str, float] = {}
        if execution is not None:
            await bind_tenant(execution.session, execution.organization.organization_id)
            stored = await provider_operational_signals(
                execution.session,
                execution.organization,
                capability=Capability.FIELD_EXTRACTION.value,
            )
            health.update(stored.health)
            quality.update(stored.quality)

        # Unknown is explicit in production routing rather than silently
        # masquerading as healthy.  It remains eligible for the first probe
        # and ranks behind providers with recent successful calls.
        for name in self._providers:
            health.setdefault(name, "unknown")
        costs = {
            name: item.estimated_cost_cents
            for name, item in self._providers.items()
            if item.estimated_cost_cents is not None
        }
        try:
            decision = route(
                Capability.FIELD_EXTRACTION,
                policy=self._policy,
                facts=DocumentFacts(language=self._language),
                signals=OperationalSignals(health=health, quality=quality, cost_cents=costs),
            )
        except Exception as error:
            await self._record_route_failure(execution, error)
            raise ExtractionProviderError(
                f"provider routing refused the run ({type(error).__name__})",
                retryable=False,
            ) from None

        ordered = (decision.provider.name, *decision.fallbacks)
        await self._audit(
            execution,
            action="provider.route_selected",
            summary={
                "capability": Capability.FIELD_EXTRACTION.value,
                "selected_provider": decision.provider.name,
                "fallback_providers": list(decision.fallbacks),
                "estimated_cost_cents": decision.estimated_cost_cents,
                "explanation": list(decision.explanation),
            },
        )

        routing_warnings = [f"provider routing: {line}" for line in decision.explanation]
        consumed_budget_cents = 0
        failed_attempt_cost = 0
        failed_attempt_had_usage = False
        failed_attempt_missing_usage = False
        failed_usage_records: list[ProviderCallUsage] = []
        last_failure: ExtractionProviderError | None = None
        configured_primary = next(iter(self._providers))
        for attempt_index, name in enumerate(ordered):
            is_fallback = name != configured_primary
            configured = self._providers[name]
            estimate = configured.estimated_cost_cents
            if estimate is None:
                estimate = 0  # router permits unknown cost only when no budget applies
            if (
                self._policy.budget_cents is not None
                and consumed_budget_cents + estimate > self._policy.budget_cents
            ):
                routing_warnings.append(
                    f"provider routing: skipped fallback {name}; cumulative estimates would "
                    f"exceed the {self._policy.budget_cents} cent budget"
                )
                continue
            provider = configured.provider
            if execution is not None:
                # Persist routing/previous-attempt evidence and release the
                # connection before the provider's network latency. Queue and
                # stage leases, not an idle SQL transaction, fence this call.
                await execution.session.commit()
            started = time.monotonic()
            try:
                if getattr(provider, "supports_repair", False):
                    default_repair = RepairPolicy()
                    remaining_budget = (
                        max(0, self._policy.budget_cents - consumed_budget_cents)
                        if self._policy.budget_cents is not None
                        else default_repair.max_cost_cents
                    )
                    repair_attempts = (
                        max(
                            1,
                            min(
                                default_repair.max_attempts,
                                remaining_budget // estimate,
                            ),
                        )
                        if self._policy.budget_cents is not None and estimate > 0
                        else default_repair.max_attempts
                    )
                    repair = await extract_with_repair(
                        cast(RepairableExtractionProvider, provider),
                        request,
                        RepairPolicy(
                            max_attempts=repair_attempts,
                            max_cost_cents=remaining_budget,
                        ),
                    )
                    result = repair.result
                else:
                    result = await provider.extract(request)
            except ExtractionProviderError as error:
                latency_ms = max(0, int((time.monotonic() - started) * 1000))
                raw_failure_cost = getattr(error, "cost_cents", 0)
                has_direct_failure_cost = hasattr(error, "cost_cents") and (
                    isinstance(raw_failure_cost, int)
                    and not isinstance(raw_failure_cost, bool)
                    and raw_failure_cost >= 0
                )
                direct_failure_cost = (
                    raw_failure_cost
                    if isinstance(raw_failure_cost, int)
                    and not isinstance(raw_failure_cost, bool)
                    and raw_failure_cost >= 0
                    else 0
                )
                attempt_records = list(error.usage_records)
                if not attempt_records:
                    declared = getattr(provider, "runtime_provenance", {})
                    model = (
                        declared.get("model")
                        if isinstance(declared, dict) and isinstance(declared.get("model"), str)
                        else None
                    )
                    attempt_records.append(
                        ProviderCallUsage(
                            provider=name,
                            model=model,
                            usage=getattr(error, "usage", None),
                            estimated_cost_cents=direct_failure_cost,
                            pricing_reference=getattr(error, "pricing_reference", None),
                            outcome="failed",
                        )
                    )
                failure_cost = sum(item.estimated_cost_cents for item in attempt_records)
                failed_usage_records.extend(attempt_records)
                error.usage_records = tuple(failed_usage_records)
                failed_attempt_cost += failure_cost
                failed_attempt_had_usage = failed_attempt_had_usage or (
                    any(item.usage is not None for item in attempt_records)
                )
                failed_attempt_missing_usage = failed_attempt_missing_usage or any(
                    item.usage is None for item in attempt_records
                )
                consumed_budget_cents += failure_cost
                if not error.usage_records_complete and not has_direct_failure_cost:
                    # A timeout/transport failure has no provider-reported
                    # actual. Reserve the configured estimate for the unknown
                    # call while retaining only known actuals in stage cost.
                    consumed_budget_cents += estimate
                await self._record_attempt(
                    execution,
                    provider=name,
                    succeeded=False,
                    fallback=is_fallback,
                    latency_ms=latency_ms,
                    cost_cents=failure_cost,
                    failure_class="retryable" if error.retryable else "terminal",
                )
                last_failure = error
                if (
                    self._policy.budget_cents is not None
                    and consumed_budget_cents > self._policy.budget_cents
                ):
                    await self._audit(
                        execution,
                        action="provider.budget_exceeded",
                        summary={
                            "provider": name,
                            "budget_cents": self._policy.budget_cents,
                            "actual_cost_cents": failed_attempt_cost,
                            "conservative_consumed_cents": consumed_budget_cents,
                            "outcome": "failed_attempts",
                        },
                    )
                    raise ExtractionProviderError(
                        "provider failures exhausted the pinned document budget",
                        retryable=False,
                        usage_records=tuple(failed_usage_records),
                        usage_records_complete=error.usage_records_complete,
                    ) from None
                if not error.retryable or attempt_index + 1 >= len(ordered):
                    error.usage_records = tuple(failed_usage_records)
                    raise
                next_name = ordered[attempt_index + 1]
                routing_warnings.append(
                    f"provider routing: {name} had a retryable failure; falling back to {next_name}"
                )
                await self._audit_fallback(execution, name, next_name, "retryable")
                continue
            except Exception:
                latency_ms = max(0, int((time.monotonic() - started) * 1000))
                declared = getattr(provider, "runtime_provenance", {})
                model = (
                    declared.get("model")
                    if isinstance(declared, dict) and isinstance(declared.get("model"), str)
                    else None
                )
                failed_usage_records.append(
                    ProviderCallUsage(
                        provider=name,
                        model=model,
                        usage=None,
                        estimated_cost_cents=0,
                        pricing_reference=None,
                        outcome="failed",
                    )
                )
                await self._record_attempt(
                    execution,
                    provider=name,
                    succeeded=False,
                    fallback=is_fallback,
                    latency_ms=latency_ms,
                    failure_class="terminal",
                )
                raise ExtractionProviderError(
                    "the provider adapter failed unexpectedly",
                    retryable=False,
                    usage_records=tuple(failed_usage_records),
                ) from None

            latency_ms = max(0, int((time.monotonic() - started) * 1000))
            violations = validate_result_against_request(request, result)
            if result.provider != name:
                violations.append(
                    f"provider result was attributed to {result.provider!r}, expected {name!r}"
                )
            if violations:
                contract_records = self._usage_records_for_result(result, failed=True)
                contract_cost = sum(item.estimated_cost_cents for item in contract_records)
                failed_usage_records.extend(contract_records)
                failed_attempt_cost += contract_cost
                consumed_budget_cents += max(contract_cost, result.cost_cents, 0)
                await self._record_attempt(
                    execution,
                    provider=name,
                    succeeded=False,
                    fallback=is_fallback,
                    latency_ms=latency_ms,
                    cost_cents=contract_cost,
                    failure_class="terminal",
                )
                last_failure = ExtractionProviderError(
                    "the provider returned output that violated the extraction contract",
                    retryable=False,
                    usage_records=tuple(failed_usage_records),
                    usage_records_complete=True,
                )
                if (
                    self._policy.budget_cents is not None
                    and consumed_budget_cents > self._policy.budget_cents
                ):
                    await self._audit(
                        execution,
                        action="provider.budget_exceeded",
                        summary={
                            "provider": name,
                            "budget_cents": self._policy.budget_cents,
                            "actual_cost_cents": failed_attempt_cost,
                            "outcome": "contract_invalid",
                        },
                    )
                    raise last_failure
                if attempt_index + 1 >= len(ordered):
                    raise last_failure
                next_name = ordered[attempt_index + 1]
                routing_warnings.append(
                    f"provider routing: {name} returned invalid contract output; falling "
                    f"back to {next_name}"
                )
                await self._audit_fallback(execution, name, next_name, "contract")
                continue

            winner_records = self._usage_records_for_result(result)
            all_usage_records = (*failed_usage_records, *winner_records)
            if (
                self._policy.budget_cents is not None
                and consumed_budget_cents + result.cost_cents > self._policy.budget_cents
            ):
                await self._record_attempt(
                    execution,
                    provider=name,
                    succeeded=True,
                    fallback=is_fallback,
                    latency_ms=latency_ms,
                    cost_cents=result.cost_cents,
                    mean_quality=self._mean_confidence(result),
                )
                await self._audit(
                    execution,
                    action="provider.budget_exceeded",
                    summary={
                        "provider": name,
                        "budget_cents": self._policy.budget_cents,
                        "actual_cost_cents": failed_attempt_cost + result.cost_cents,
                    },
                )
                raise ExtractionProviderError(
                    "the provider exceeded the pinned document budget",
                    retryable=False,
                    usage_records=all_usage_records,
                    usage_records_complete=True,
                )

            await self._record_attempt(
                execution,
                provider=name,
                succeeded=True,
                fallback=is_fallback,
                latency_ms=latency_ms,
                cost_cents=result.cost_cents,
                mean_quality=self._mean_confidence(result),
            )
            declared = getattr(provider, "runtime_provenance", {})
            self._selected_provenance.set(
                {
                    "selected_adapter": (dict(declared) if isinstance(declared, dict) else {}),
                    "configured_chain": list(self._providers),
                }
            )
            if failed_attempt_cost > 0 or failed_attempt_had_usage:
                routing_warnings.append(
                    "provider routing: prior failed provider calls incurred paid usage; "
                    f"{failed_attempt_cost} cents is included in the stage total, while "
                    "each provider call keeps separate token and rate-card attribution"
                )
            if failed_attempt_missing_usage:
                routing_warnings.append(
                    "provider routing: a failed call returned no token usage metadata; "
                    "its zero ledger estimate is not proof of zero invoice cost, so the "
                    "configured estimate was reserved for budget enforcement"
                )
            return replace(
                result,
                cost_cents=failed_attempt_cost + result.cost_cents,
                warnings=(*routing_warnings, *result.warnings),
                usage_records=all_usage_records,
            )

        if last_failure is not None:
            raise last_failure
        raise ExtractionProviderError(
            "no configured fallback fits the remaining document budget",
            retryable=False,
            usage_records=tuple(failed_usage_records),
        )

    @staticmethod
    def _usage_records_for_result(
        result: ExtractionResult, *, failed: bool = False
    ) -> tuple[ProviderCallUsage, ...]:
        records = result.usage_records or (
            ProviderCallUsage(
                provider=result.provider,
                model=result.model,
                usage=result.usage,
                estimated_cost_cents=max(0, result.cost_cents),
                pricing_reference=result.pricing_reference,
                outcome="succeeded",
            ),
        )
        if not failed:
            return records
        return tuple(
            replace(item, outcome="failed") if item.outcome == "succeeded" else item
            for item in records
        )

    @staticmethod
    def _mean_confidence(result: ExtractionResult) -> float | None:
        if not result.fields:
            return None
        return sum(field.confidence for field in result.fields) / len(result.fields)

    async def _record_attempt(
        self,
        execution: RoutingExecutionContext | None,
        *,
        provider: str,
        succeeded: bool,
        fallback: bool,
        latency_ms: int,
        cost_cents: int = 0,
        mean_quality: float | None = None,
        failure_class: str | None = None,
    ) -> None:
        if execution is None:
            return
        await bind_tenant(execution.session, execution.organization.organization_id)
        await record_provider_attempt(
            execution.session,
            execution.organization,
            capability=Capability.FIELD_EXTRACTION.value,
            provider=provider,
            succeeded=succeeded,
            fallback=fallback,
            latency_ms=latency_ms,
            cost_cents=cost_cents,
            mean_quality=mean_quality,
            failure_class=failure_class,
        )

    async def _audit(
        self,
        execution: RoutingExecutionContext | None,
        *,
        action: str,
        summary: dict[str, Any],
    ) -> None:
        if execution is None:
            return
        await bind_tenant(execution.session, execution.organization.organization_id)
        await record_audit_event(
            execution.session,
            actor_type=ActorType.SYSTEM,
            actor_id=ACTOR,
            action=action,
            target_type="processing_run",
            target_id=str(execution.run.id),
            organization_id=execution.organization.organization_id,
            summary={
                **summary,
                "stage_run_id": str(execution.stage_run.id),
                "provider_policy_version_id": (
                    str(execution.run.provider_policy_version_id)
                    if execution.run.provider_policy_version_id
                    else None
                ),
            },
        )

    async def _audit_fallback(
        self,
        execution: RoutingExecutionContext | None,
        provider: str,
        fallback: str,
        reason_class: str,
    ) -> None:
        await self._audit(
            execution,
            action="provider.fallback_triggered",
            summary={
                "provider": provider,
                "fallback_provider": fallback,
                "reason_class": reason_class,
            },
        )

    async def _record_route_failure(
        self, execution: RoutingExecutionContext | None, error: Exception
    ) -> None:
        explanation = getattr(error, "explanation", ())
        await self._audit(
            execution,
            action="provider.route_refused",
            summary={
                "error_class": type(error).__name__,
                "explanation": list(explanation) if isinstance(explanation, tuple) else [],
            },
        )


__all__ = [
    "MAX_RUNTIME_PROVIDER_CHAIN",
    "ConfiguredExtractionProvider",
    "RoutedExtractionProvider",
    "RoutingExecutionContext",
]
