"""Hosted Claude extraction adapter tests (AIO-008): the PRC-006 contract
suite over a mocked Messages endpoint, request discipline (no tools,
determinism, Messages shape, auth header), honest output handling shared
with the local adapter, and error classification (mocked errors, repair,
timeout, rate limit)."""

import json
import uuid
from typing import Any

import httpx
import pytest

from soa_worker.anthropic_extraction import (
    CAPABILITY_WARNING,
    DEFAULT_API_VERSION,
    PROVIDER_NAME,
    AnthropicExtractionProvider,
    register_anthropic_extraction,
)
from soa_worker.extraction.contract import ExtractionProviderContract
from soa_worker.extraction.provider import (
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    FieldSpec,
    PageInput,
    validate_result_against_request,
)
from soa_worker.providers import Capability, provider_info, unregister_provider

ENDPOINT = "https://api.anthropic.com/v1/messages"
DOC_ID = uuid.UUID("1f4b8a00-0000-4000-8000-0000000000ee")
PAGE_TEXT = "PURCHASE ORDER PO-4711\nBuyer: Acme GmbH\nCurrency: EUR"


def extraction_request(
    fields: tuple[FieldSpec, ...] = (
        FieldSpec(key="po_number", field_type="text"),
        FieldSpec(key="buyer_name", field_type="text"),
        FieldSpec(key="ship_date", field_type="date"),
    ),
) -> ExtractionRequest:
    return ExtractionRequest(
        document_id=DOC_ID,
        document_sha256="e" * 64,
        content_type="application/pdf",
        pages=(PageInput(page_number=1, width_px=1240, height_px=1754, text=PAGE_TEXT),),
        fields=fields,
    )


def model_answer(fields: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"content": [{"type": "text", "text": json.dumps({"fields": fields})}]},
    )


DEFAULT_FIELDS = [
    {
        "key": "po_number",
        "value": "PO-4711",
        "confidence": 0.92,
        "page_number": 1,
        "quote": "PURCHASE ORDER PO-4711",
        "row_index": None,
    },
    {
        "key": "buyer_name",
        "value": "Acme GmbH",
        "confidence": 0.88,
        "page_number": 1,
        "quote": "Buyer: Acme GmbH",
        "row_index": None,
    },
    {"key": "ship_date", "value": None},
]


def provider_with(handler: Any = None) -> AnthropicExtractionProvider:
    transport = httpx.MockTransport(handler or (lambda _r: model_answer(DEFAULT_FIELDS)))
    return AnthropicExtractionProvider(
        api_key="sk-ant-test",
        model="test-model",
        client=httpx.AsyncClient(transport=transport),
    )


class TestClaudeContract(ExtractionProviderContract):
    async def make_provider(self) -> ExtractionProvider:
        return provider_with()

    def contract_request(self) -> ExtractionRequest:
        return extraction_request()


class TestRequestDiscipline:
    async def test_messages_shape_auth_and_no_tools(self) -> None:
        seen_payloads: list[dict[str, Any]] = []
        seen_headers: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_payloads.append(json.loads(request.content))
            seen_headers.append(request.headers)
            return model_answer(DEFAULT_FIELDS)

        await provider_with(handler).extract(extraction_request())
        (payload,) = seen_payloads
        (headers,) = seen_headers
        # Messages API shape: system is a top-level field, one user turn.
        assert "tools" not in payload
        assert payload["temperature"] == 0
        assert isinstance(payload["system"], str)
        assert "SECURITY RULES" in payload["system"]
        assert [m["role"] for m in payload["messages"]] == ["user"]
        # The document text rides in the user turn (JSON-encoded), not the
        # system field.
        assert "PURCHASE ORDER PO-4711" in payload["messages"][0]["content"]
        assert "PURCHASE ORDER PO-4711" not in payload["system"]
        # Auth + version headers are present; the key is never in the body.
        assert headers["x-api-key"] == "sk-ant-test"
        assert headers["anthropic-version"] == DEFAULT_API_VERSION
        assert "sk-ant-test" not in json.dumps(payload)

    def test_a_missing_key_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError):
            AnthropicExtractionProvider(api_key="", model="m")


class TestHonestOutputHandling:
    async def test_found_values_carry_page_level_evidence_with_the_quote(self) -> None:
        request = extraction_request()
        result = await provider_with().extract(request)
        assert validate_result_against_request(request, result) == []
        by_key = {field.field_key: field for field in result.fields}
        po = by_key["po_number"]
        assert po.raw_value == "PO-4711"
        assert po.confidence == 0.92
        (span,) = po.evidence
        assert span.quote == "PURCHASE ORDER PO-4711"
        assert span.polygon == ((0.0, 0.0), (1240.0, 0.0), (1240.0, 1754.0), (0.0, 1754.0))

    async def test_null_values_are_absent_without_evidence(self) -> None:
        result = await provider_with().extract(extraction_request())
        by_key = {field.field_key: field for field in result.fields}
        assert by_key["ship_date"].raw_value is None
        assert by_key["ship_date"].evidence == ()

    async def test_result_names_the_provider_and_carries_the_warning(self) -> None:
        result = await provider_with().extract(extraction_request())
        assert result.provider == PROVIDER_NAME
        assert CAPABILITY_WARNING in result.warnings

    async def test_multiple_text_blocks_are_concatenated(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            payload = {"fields": DEFAULT_FIELDS}
            dumped = json.dumps(payload)
            half = len(dumped) // 2
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "text", "text": dumped[:half]},
                        {"type": "text", "text": dumped[half:]},
                    ]
                },
            )

        result = await provider_with(handler).extract(extraction_request())
        by_key = {field.field_key: field for field in result.fields}
        assert by_key["po_number"].raw_value == "PO-4711"


class TestRepair:
    async def test_repair_hint_is_appended_as_an_extra_user_turn(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return model_answer(DEFAULT_FIELDS)

        await provider_with(handler).extract(extraction_request(), repair_hint="was not JSON")
        (payload,) = seen
        assert len(payload["messages"]) == 2
        assert "was not JSON" in payload["messages"][1]["content"]


class TestErrorClassification:
    async def test_unparseable_model_json_is_retryable_and_safe(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "SECRET-DOC not json {"}]}
            )

        with pytest.raises(ExtractionProviderError) as caught:
            await provider_with(handler).extract(extraction_request())
        assert caught.value.retryable is True
        assert "SECRET-DOC" not in str(caught.value)

    async def test_server_errors_and_rate_limits_are_retryable(self) -> None:
        for status in (500, 503, 429):
            with pytest.raises(ExtractionProviderError) as caught:
                await provider_with(lambda _r, s=status: httpx.Response(s)).extract(
                    extraction_request()
                )
            assert caught.value.retryable is True, f"status {status}"

    async def test_auth_and_client_errors_are_terminal(self) -> None:
        for status in (400, 401, 403):
            with pytest.raises(ExtractionProviderError) as caught:
                await provider_with(lambda _r, s=status: httpx.Response(s)).extract(
                    extraction_request()
                )
            assert caught.value.retryable is False, f"status {status}"

    async def test_timeouts_are_retryable(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow model")

        with pytest.raises(ExtractionProviderError) as caught:
            await provider_with(handler).extract(extraction_request())
        assert caught.value.retryable is True
        assert "did not answer" in str(caught.value)


class TestHostedProfile:
    def test_registration_declares_third_party_processing(self) -> None:
        register_anthropic_extraction("sk-ant-test")
        try:
            info = provider_info(Capability.FIELD_EXTRACTION, PROVIDER_NAME)
            # The data policy tells the truth: hosted, third-party — so
            # tenant policy can refuse it when local-only is required.
            assert info.data_policy.processing_region == "us"
            assert info.data_policy.sends_content_to_third_party is True
        finally:
            unregister_provider(Capability.FIELD_EXTRACTION, PROVIDER_NAME)

    def test_an_unconfigured_deployment_has_no_claude_provider(self) -> None:
        from soa_worker.providers import UnknownProviderError

        with pytest.raises(UnknownProviderError):
            provider_info(Capability.FIELD_EXTRACTION, PROVIDER_NAME)
