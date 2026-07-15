"""Hosted Gemini extraction adapter tests (AIO-009): the PRC-006 contract
suite over a mocked generateContent endpoint, request discipline (no
tools, determinism, system_instruction shape, x-goog-api-key auth),
honest output shared with the other adapters, and error classification."""

import json
import uuid
from typing import Any

import httpx
import pytest

from soa_worker.extraction.contract import ExtractionProviderContract
from soa_worker.extraction.provider import (
    ExtractionProvider,
    ExtractionProviderError,
    ExtractionRequest,
    FieldSpec,
    PageInput,
    validate_result_against_request,
)
from soa_worker.gemini_extraction import (
    CAPABILITY_WARNING,
    PROVIDER_NAME,
    GeminiExtractionProvider,
    register_gemini_extraction,
)
from soa_worker.model_extraction_result import ModelOutputInvalidError
from soa_worker.providers import Capability, create_provider, provider_info, unregister_provider

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
        json={
            "candidates": [{"content": {"parts": [{"text": json.dumps({"fields": fields})}]}}],
            "usageMetadata": {
                "promptTokenCount": 20_000,
                "candidatesTokenCount": 10_000,
                "totalTokenCount": 31_000,
            },
        },
    )


DEFAULT_FIELDS = [
    {
        "key": "po_number",
        "value": "PO-4711",
        "confidence": 0.9,
        "page_number": 1,
        "quote": "PURCHASE ORDER PO-4711",
        "row_index": None,
    },
    {"key": "buyer_name", "value": "Acme GmbH", "confidence": 0.8, "page_number": 1},
    {"key": "ship_date", "value": None},
]


def provider_with(handler: Any = None) -> GeminiExtractionProvider:
    transport = httpx.MockTransport(handler or (lambda _r: model_answer(DEFAULT_FIELDS)))
    return GeminiExtractionProvider(
        api_key="AIza-test", model="test-model", client=httpx.AsyncClient(transport=transport)
    )


class TestGeminiContract(ExtractionProviderContract):
    async def make_provider(self) -> ExtractionProvider:
        return provider_with()

    def contract_request(self) -> ExtractionRequest:
        return extraction_request()


class TestRequestDiscipline:
    async def test_generatecontent_shape_auth_and_no_tools(self) -> None:
        payloads: list[dict[str, Any]] = []
        headers: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            headers.append(request.headers)
            return model_answer(DEFAULT_FIELDS)

        await provider_with(handler).extract(extraction_request())
        (payload,) = payloads
        (sent_headers,) = headers
        assert "tools" not in payload
        assert payload["generationConfig"]["temperature"] == 0
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        # System instruction is separate from the user content.
        system = payload["system_instruction"]["parts"][0]["text"]
        assert "SECURITY RULES" in system
        assert "PURCHASE ORDER PO-4711" not in system
        assert [c["role"] for c in payload["contents"]] == ["user"]
        assert "PURCHASE ORDER PO-4711" in payload["contents"][0]["parts"][0]["text"]
        # The key travels as a header, never in the body or URL.
        assert sent_headers["x-goog-api-key"] == "AIza-test"
        assert "AIza-test" not in json.dumps(payload)

    def test_missing_key_refused_at_construction(self) -> None:
        with pytest.raises(ValueError):
            GeminiExtractionProvider(api_key="", model="m")


class TestHonestOutput:
    async def test_found_values_carry_page_evidence(self) -> None:
        request = extraction_request()
        result = await provider_with().extract(request)
        assert validate_result_against_request(request, result) == []
        by_key = {f.field_key: f for f in result.fields}
        assert by_key["po_number"].raw_value == "PO-4711"
        assert by_key["ship_date"].raw_value is None
        assert result.provider == PROVIDER_NAME
        assert CAPABILITY_WARNING in result.warnings
        assert result.usage is not None
        assert result.usage.as_dict() == {
            "input_tokens": 20_000,
            "output_tokens": 10_000,
            "total_tokens": 31_000,
        }
        assert result.cost_cents == 1
        assert result.pricing_reference == "soa-rate-card-v1:google:gemini-2.0-flash"

    async def test_multiple_parts_are_concatenated(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            dumped = json.dumps({"fields": DEFAULT_FIELDS})
            half = len(dumped) // 2
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": dumped[:half]}, {"text": dumped[half:]}]}}
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 20_000,
                        "candidatesTokenCount": 10_000,
                        "totalTokenCount": 31_000,
                    },
                },
            )

        result = await provider_with(handler).extract(extraction_request())
        by_key = {f.field_key: f for f in result.fields}
        assert by_key["po_number"].raw_value == "PO-4711"


class TestErrors:
    async def test_unparseable_json_is_retryable_and_safe(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "candidates": [{"content": {"parts": [{"text": "SECRET not json {"}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 5,
                        "candidatesTokenCount": 2,
                        "totalTokenCount": 7,
                    },
                },
            )

        with pytest.raises(ModelOutputInvalidError) as caught:
            await provider_with(handler).extract(extraction_request())
        assert caught.value.retryable is True
        assert "SECRET" not in str(caught.value)
        assert caught.value.usage is not None and caught.value.usage.total_tokens == 7

    async def test_missing_or_malformed_usage_is_safe_and_retryable(self) -> None:
        response = {"candidates": [{"content": {"parts": [{"text": json.dumps({"fields": []})}]}}]}
        with pytest.raises(ExtractionProviderError) as caught:
            await provider_with(lambda _r: httpx.Response(200, json=response)).extract(
                extraction_request()
            )
        assert caught.value.retryable is True
        assert "unexpected response shape" in str(caught.value)

    async def test_server_and_rate_limit_retryable(self) -> None:
        for status in (500, 503, 429):
            with pytest.raises(ExtractionProviderError) as caught:
                await provider_with(lambda _r, s=status: httpx.Response(s)).extract(
                    extraction_request()
                )
            assert caught.value.retryable is True, f"status {status}"

    async def test_client_errors_terminal(self) -> None:
        for status in (400, 401, 403):
            with pytest.raises(ExtractionProviderError) as caught:
                await provider_with(lambda _r, s=status: httpx.Response(s)).extract(
                    extraction_request()
                )
            assert caught.value.retryable is False, f"status {status}"


class TestHostedProfile:
    def test_registration_declares_third_party(self) -> None:
        register_gemini_extraction("AIza-test")
        try:
            info = provider_info(Capability.FIELD_EXTRACTION, PROVIDER_NAME)
            assert info.data_policy.sends_content_to_third_party is True
        finally:
            unregister_provider(Capability.FIELD_EXTRACTION, PROVIDER_NAME)

    def test_unconfigured_deployment_has_no_gemini(self) -> None:
        from soa_worker.providers import UnknownProviderError

        with pytest.raises(UnknownProviderError):
            provider_info(Capability.FIELD_EXTRACTION, PROVIDER_NAME)

    def test_explicit_tenant_credential_never_falls_back_to_deployment_key(self) -> None:
        register_gemini_extraction("deployment-key")
        try:
            assert isinstance(
                create_provider(Capability.FIELD_EXTRACTION, PROVIDER_NAME),
                GeminiExtractionProvider,
            )
            with pytest.raises(ValueError, match="needs an API key"):
                create_provider(
                    Capability.FIELD_EXTRACTION,
                    PROVIDER_NAME,
                    credential_value="",
                )
            tenant = create_provider(
                Capability.FIELD_EXTRACTION,
                PROVIDER_NAME,
                credential_value="tenant-key",
            )
            assert isinstance(tenant, GeminiExtractionProvider)
        finally:
            unregister_provider(Capability.FIELD_EXTRACTION, PROVIDER_NAME)

    def test_tenant_only_registration_requires_runtime_credential(self) -> None:
        register_gemini_extraction(None)
        try:
            with pytest.raises(ValueError, match="needs an API key"):
                create_provider(Capability.FIELD_EXTRACTION, PROVIDER_NAME)
            assert isinstance(
                create_provider(
                    Capability.FIELD_EXTRACTION,
                    PROVIDER_NAME,
                    credential_value="tenant-key",
                ),
                GeminiExtractionProvider,
            )
        finally:
            unregister_provider(Capability.FIELD_EXTRACTION, PROVIDER_NAME)
