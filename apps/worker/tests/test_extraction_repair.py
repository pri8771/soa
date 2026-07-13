"""Repair and fallback policy tests (AIO-012): invalid JSON / wrong
type / missing field repaired end-to-end through the real adapter,
attempt and cost ceilings, rate limits deferring to the queue, and the
honest manual-review fallback."""

import json
import uuid
from typing import Any

import httpx
import pytest

from soa_worker.extraction.provider import (
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
    FieldSpec,
    PageInput,
)
from soa_worker.extraction_repair import (
    FALLBACK_MANUAL_REVIEW,
    RepairPolicy,
    extract_with_repair,
)
from soa_worker.llm_extraction import (
    ModelOutputInvalidError,
    OpenAiCompatibleExtractionProvider,
)

DOC_ID = uuid.UUID("6a2c7b00-0000-4000-8000-0000000000ff")


def request() -> ExtractionRequest:
    return ExtractionRequest(
        document_id=DOC_ID,
        document_sha256="f" * 64,
        content_type="application/pdf",
        pages=(PageInput(page_number=1, width_px=1000, height_px=1400, text="PO-4711"),),
        fields=(FieldSpec(key="po_number", field_type="text"),),
    )


def response_with(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


GOOD = json.dumps(
    {"fields": [{"key": "po_number", "value": "PO-4711", "confidence": 0.9, "page_number": 1}]}
)


def adapter_with_scripted_answers(answers: list[str]) -> tuple[Any, list[dict[str, Any]]]:
    """A real adapter whose endpoint answers from a script; returns the
    adapter plus every wire payload for inspection."""
    wire: list[dict[str, Any]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(http_request.content))
        return response_with(answers[min(len(wire) - 1, len(answers) - 1)])

    provider = OpenAiCompatibleExtractionProvider(
        endpoint="http://llm.local/v1/chat/completions",
        model="test-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return provider, wire


class _ScriptedProvider:
    """Raises/returns from a script — for policy-shape tests."""

    name = "scripted"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.hints: list[str | None] = []

    async def extract(
        self, request: ExtractionRequest, *, repair_hint: str | None = None
    ) -> ExtractionResult:
        self.hints.append(repair_hint)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        assert isinstance(step, ExtractionResult)
        return step


GOOD_RESULT = ExtractionResult(provider="scripted", fields=())


class TestRepairEndToEnd:
    @pytest.mark.parametrize(
        ("bad_answer", "reason_fragment"),
        [
            ("not json at all {", "not valid JSON"),
            (json.dumps({"answers": []}), 'missing the "fields" key'),
            (json.dumps({"fields": "nope"}), "must be a JSON array"),
        ],
    )
    async def test_invalid_shapes_are_repaired_with_the_reason_on_the_wire(
        self, bad_answer: str, reason_fragment: str
    ) -> None:
        provider, wire = adapter_with_scripted_answers([bad_answer, GOOD])
        outcome = await extract_with_repair(provider, request())
        assert outcome.fallback is None
        by_key = {field.field_key: field for field in outcome.result.fields}
        assert by_key["po_number"].raw_value == "PO-4711"
        assert [record.outcome for record in outcome.attempts] == ["invalid_output", "ok"]
        # The second wire request carries the repair hint as an extra
        # user turn naming the reason.
        assert len(wire) == 2
        repair_turn = wire[1]["messages"][-1]
        assert repair_turn["role"] == "user"
        assert reason_fragment in repair_turn["content"]
        assert any("repaired" in warning for warning in outcome.result.warnings)

    async def test_a_clean_first_answer_needs_no_repair(self) -> None:
        provider, wire = adapter_with_scripted_answers([GOOD])
        outcome = await extract_with_repair(provider, request())
        assert outcome.fallback is None
        assert [record.outcome for record in outcome.attempts] == ["ok"]
        assert len(wire) == 1
        assert not any("repaired" in warning for warning in outcome.result.warnings)


class TestBounds:
    async def test_attempts_are_bounded_and_exhaustion_falls_back_to_review(self) -> None:
        provider, wire = adapter_with_scripted_answers(["broken {"])
        outcome = await extract_with_repair(
            provider, request(), RepairPolicy(max_attempts=3, max_cost_cents=1000)
        )
        assert len(wire) == 3  # the hard bound; no infinite loop
        assert outcome.fallback == FALLBACK_MANUAL_REVIEW
        (field,) = outcome.result.fields
        assert field.field_key == "po_number"
        assert field.raw_value is None  # honest absence, never fabrication
        assert field.evidence == ()
        assert any("manual review" in warning for warning in outcome.result.warnings)
        assert [record.attempt_number for record in outcome.attempts] == [1, 2, 3]

    async def test_the_cost_ceiling_stops_repair_before_the_attempt_ceiling(self) -> None:
        scripted = _ScriptedProvider(
            [
                ModelOutputInvalidError("bad", cost_cents=30),
                ModelOutputInvalidError("bad", cost_cents=30),
                GOOD_RESULT,  # never reached: budget dies first
            ]
        )
        outcome = await extract_with_repair(
            scripted, request(), RepairPolicy(max_attempts=5, max_cost_cents=50)
        )
        assert outcome.fallback == FALLBACK_MANUAL_REVIEW
        assert len(scripted.hints) == 2  # stopped by cost, not attempts
        assert any(record.outcome == "cost_exhausted" for record in outcome.attempts)

    async def test_successful_repair_accounts_the_failed_attempts_cost(self) -> None:
        scripted = _ScriptedProvider(
            [
                ModelOutputInvalidError("bad", cost_cents=10),
                ExtractionResult(provider="scripted", fields=(), cost_cents=15),
            ]
        )
        outcome = await extract_with_repair(scripted, request())
        assert outcome.result.cost_cents == 25


class TestClassification:
    async def test_rate_limits_defer_to_the_queue_not_the_repair_loop(self) -> None:
        rate_limited = ExtractionProviderError("status 429", retryable=True)
        scripted = _ScriptedProvider([rate_limited, GOOD_RESULT])
        with pytest.raises(ExtractionProviderError) as caught:
            await extract_with_repair(scripted, request())
        assert caught.value.retryable is True
        assert len(scripted.hints) == 1  # no second call: the queue backs off

    async def test_terminal_errors_propagate(self) -> None:
        scripted = _ScriptedProvider([ExtractionProviderError("bad config", retryable=False)])
        with pytest.raises(ExtractionProviderError) as caught:
            await extract_with_repair(scripted, request())
        assert caught.value.retryable is False

    async def test_the_repair_hint_is_the_safe_reason(self) -> None:
        scripted = _ScriptedProvider(
            [ModelOutputInvalidError("the response was not valid JSON"), GOOD_RESULT]
        )
        await extract_with_repair(scripted, request())
        assert scripted.hints == [None, "the response was not valid JSON"]
