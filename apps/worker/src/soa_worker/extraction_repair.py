"""Schema repair and fallback policy for model extraction (AIO-012).

When a model answers in the wrong shape (invalid JSON, wrong types, a
missing ``fields`` key), the answer is often one nudge away from valid
— so the policy re-asks with the safe reason as a repair hint. The loop
is HARD-BOUNDED two ways and can never spin:

- an attempt ceiling (``max_attempts`` total model calls), and
- a cost ceiling (``max_cost_cents`` across attempts — failed calls
  still burn tokens, and adapters report what each one cost).

Every attempt is recorded with its outcome and safe detail; the record
travels on the final result's warnings so the stage run shows the whole
story.

Failure classification decides what happens next:

- ``ModelOutputInvalidError`` → repair (re-ask with the reason);
- rate limits / timeouts / transport errors (retryable
  ``ExtractionProviderError``) → RE-RAISED immediately: spinning on a
  rate limit here would burn quota, and JOB-005's queue-level retry
  with backoff is the right tool;
- terminal errors → re-raised;
- ceilings exhausted → the FALLBACK: an honest all-absent result
  (every requested field ``raw_value=None``, no evidence, confidence
  0.0) flagged ``fallback="manual_review"`` — absence plus the PRC-011
  confidence policy routes the document to human review instead of
  fabricating values or dead-lettering recoverable work.
"""

from dataclasses import dataclass
from typing import Protocol

from soa_worker.extraction.provider import (
    ExtractedField,
    ExtractionRequest,
    ExtractionResult,
)
from soa_worker.llm_extraction import ModelOutputInvalidError

FALLBACK_MANUAL_REVIEW = "manual_review"


class RepairableExtractionProvider(Protocol):
    """An extraction provider with the repair channel (the AIO-007
    adapter satisfies this; the hosted AIO-008 adapter will too)."""

    @property
    def name(self) -> str: ...

    async def extract(
        self, request: ExtractionRequest, *, repair_hint: str | None = None
    ) -> ExtractionResult: ...


@dataclass(frozen=True)
class RepairPolicy:
    #: Total model calls, including the first. The hard no-infinite-loop bound.
    max_attempts: int = 3
    #: Cost ceiling across ALL attempts (failed calls included).
    max_cost_cents: int = 50

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("the policy must allow at least one attempt")
        if self.max_cost_cents < 0:
            raise ValueError("the cost ceiling cannot be negative")


@dataclass(frozen=True)
class AttemptRecord:
    attempt_number: int
    outcome: str  # ok | invalid_output | cost_exhausted
    detail: str


@dataclass(frozen=True)
class RepairOutcome:
    result: ExtractionResult
    attempts: tuple[AttemptRecord, ...]
    #: None when the model eventually answered validly; the fallback
    #: route otherwise.
    fallback: str | None


def _fallback_result(
    provider_name: str, request: ExtractionRequest, attempts: list[AttemptRecord]
) -> ExtractionResult:
    """Honest exhaustion: every requested field explicitly absent."""
    fields = tuple(
        ExtractedField(field_key=spec.key, raw_value=None, confidence=0.0)
        for spec in request.fields
    )
    story = "; ".join(f"attempt {a.attempt_number}: {a.detail}" for a in attempts)
    return ExtractionResult(
        provider=provider_name,
        fields=fields,
        warnings=(
            "model output could not be repaired within the policy bounds — "
            f"the document needs manual review ({story})",
        ),
    )


async def extract_with_repair(
    provider: RepairableExtractionProvider,
    request: ExtractionRequest,
    policy: RepairPolicy | None = None,
) -> RepairOutcome:
    """Run one extraction under the repair policy. See module docstring
    for the classification rules; retryable transport/rate-limit errors
    propagate to the caller (the job queue owns that backoff)."""
    effective = policy or RepairPolicy()
    attempts: list[AttemptRecord] = []
    spent_cents = 0
    repair_hint: str | None = None

    for attempt_number in range(1, effective.max_attempts + 1):
        try:
            result = await provider.extract(request, repair_hint=repair_hint)
        except ModelOutputInvalidError as invalid:
            spent_cents += invalid.cost_cents
            attempts.append(
                AttemptRecord(
                    attempt_number=attempt_number,
                    outcome="invalid_output",
                    detail=invalid.reason,
                )
            )
            repair_hint = invalid.reason
            if spent_cents >= effective.max_cost_cents:
                attempts.append(
                    AttemptRecord(
                        attempt_number=attempt_number,
                        outcome="cost_exhausted",
                        detail=(
                            f"{spent_cents} of {effective.max_cost_cents} cents spent — "
                            "no further repair attempts"
                        ),
                    )
                )
                break
            continue
        # Success: record it and stop.
        attempts.append(
            AttemptRecord(attempt_number=attempt_number, outcome="ok", detail="valid output")
        )
        merged = result
        if len(attempts) > 1:
            merged = ExtractionResult(
                provider=result.provider,
                fields=result.fields,
                model=result.model,
                cost_cents=result.cost_cents + spent_cents,
                warnings=(
                    *result.warnings,
                    f"valid output after {len(attempts)} attempts ({len(attempts) - 1} repaired)",
                ),
                instruction_reference=result.instruction_reference,
            )
        return RepairOutcome(result=merged, attempts=tuple(attempts), fallback=None)

    return RepairOutcome(
        result=_fallback_result(provider.name, request, attempts),
        attempts=tuple(attempts),
        fallback=FALLBACK_MANUAL_REVIEW,
    )


__all__ = [
    "FALLBACK_MANUAL_REVIEW",
    "AttemptRecord",
    "RepairOutcome",
    "RepairPolicy",
    "RepairableExtractionProvider",
    "extract_with_repair",
]
