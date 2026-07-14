"""Local OpenAI-compatible extraction adapter tests (AIO-007): the
PRC-006 contract suite over a mocked endpoint, request discipline (no
tools, pinned determinism knobs, structured schema), honest handling of
model output, and error classification."""

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
from soa_worker.llm_extraction import (
    CAPABILITY_WARNING,
    PROVIDER_NAME,
    OpenAiCompatibleExtractionProvider,
    register_hosted_openai_extraction,
    register_local_llm_extraction,
)
from soa_worker.providers import Capability, provider_info, unregister_provider

ENDPOINT = "http://llm.local/v1/chat/completions"
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
        json={"choices": [{"message": {"content": json.dumps({"fields": fields})}}]},
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


def provider_with(
    handler: Any = None,
) -> OpenAiCompatibleExtractionProvider:
    transport = httpx.MockTransport(handler or (lambda _r: model_answer(DEFAULT_FIELDS)))
    return OpenAiCompatibleExtractionProvider(
        endpoint=ENDPOINT,
        model="test-model",
        client=httpx.AsyncClient(transport=transport),
    )


class TestLocalLlmContract(ExtractionProviderContract):
    async def make_provider(self) -> ExtractionProvider:
        return provider_with()

    def contract_request(self) -> ExtractionRequest:
        return extraction_request()


class TestRequestDiscipline:
    async def test_the_request_carries_no_tools_and_pins_determinism(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return model_answer(DEFAULT_FIELDS)

        await provider_with(handler).extract(extraction_request())
        (payload,) = seen
        assert "tools" not in payload
        assert "tool_choice" not in payload
        assert payload["temperature"] == 0
        assert payload["seed"] == 7
        assert payload["response_format"] == {"type": "json_object"}
        user = json.loads(payload["messages"][1]["content"])
        assert [f["key"] for f in user["fields_to_extract"]] == [
            "po_number",
            "buyer_name",
            "ship_date",
        ]
        assert user["document_pages"][0]["text"] == PAGE_TEXT

    def test_enum_fields_carry_their_allowed_values(self) -> None:
        provider = provider_with()
        payload, _ = provider.build_payload(
            extraction_request(
                fields=(FieldSpec(key="currency", field_type="enum", enum_values=("EUR", "USD")),)
            )
        )
        user = json.loads(payload["messages"][1]["content"])
        assert user["fields_to_extract"][0]["allowed_values"] == ["EUR", "USD"]

    async def test_instructions_flow_through_the_safe_builder_with_their_reference(self) -> None:
        from soa_worker.model_request_builder import TENANT_SECTION_HEADER

        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return model_answer(DEFAULT_FIELDS)

        provider = OpenAiCompatibleExtractionProvider(
            endpoint=ENDPOINT,
            model="test-model",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            instructions={"instructions": "Prefer the header block."},
            instruction_reference="instruction:abc:v3",
        )
        result = await provider.extract(extraction_request())
        (payload,) = seen
        system = payload["messages"][0]["content"]
        assert "SECURITY RULES" in system
        assert TENANT_SECTION_HEADER in system
        assert "Prefer the header block." in system
        assert result.instruction_reference == "instruction:abc:v3"


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
        # Full-page polygon: honest page-level evidence, no invented boxes.
        assert span.polygon == ((0.0, 0.0), (1240.0, 0.0), (1240.0, 1754.0), (0.0, 1754.0))

    async def test_null_values_are_absent_without_evidence(self) -> None:
        result = await provider_with().extract(extraction_request())
        by_key = {field.field_key: field for field in result.fields}
        assert by_key["ship_date"].raw_value is None
        assert by_key["ship_date"].evidence == ()

    async def test_every_result_carries_the_capability_warning(self) -> None:
        result = await provider_with().extract(extraction_request())
        assert CAPABILITY_WARNING in result.warnings

    async def test_confidence_is_clamped_to_the_unit_interval(self) -> None:
        fields = [
            {"key": "po_number", "value": "PO-4711", "confidence": 1.7, "page_number": 1},
            {"key": "buyer_name", "value": "Acme GmbH", "confidence": -3, "page_number": 1},
        ]
        result = await provider_with(lambda _r: model_answer(fields)).extract(extraction_request())
        by_key = {field.field_key: field for field in result.fields}
        assert by_key["po_number"].confidence == 1.0
        assert by_key["buyer_name"].confidence == 0.0

    async def test_unrequested_fields_are_dropped_with_a_warning(self) -> None:
        fields = [
            *DEFAULT_FIELDS,
            {"key": "bank_account", "value": "DE00 1234", "confidence": 0.9, "page_number": 1},
        ]
        request = extraction_request()
        result = await provider_with(lambda _r: model_answer(fields)).extract(request)
        assert validate_result_against_request(request, result) == []
        assert all(field.field_key != "bank_account" for field in result.fields)
        assert any("bank_account" in warning for warning in result.warnings)

    async def test_a_page_the_request_lacks_yields_no_evidence(self) -> None:
        fields = [{"key": "po_number", "value": "PO-4711", "confidence": 0.9, "page_number": 9}]
        request = extraction_request()
        result = await provider_with(lambda _r: model_answer(fields)).extract(request)
        assert validate_result_against_request(request, result) == []
        by_key = {field.field_key: field for field in result.fields}
        assert by_key["po_number"].evidence == ()


class TestErrorClassification:
    async def test_unparseable_model_json_is_retryable_and_safe(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "SECRET-DOC-TEXT not json {"}}]},
            )

        with pytest.raises(ExtractionProviderError) as caught:
            await provider_with(handler).extract(extraction_request())
        assert caught.value.retryable is True
        assert "SECRET-DOC-TEXT" not in str(caught.value)  # model output never leaks

    async def test_server_errors_and_rate_limits_are_retryable(self) -> None:
        for status in (500, 503, 429):
            with pytest.raises(ExtractionProviderError) as caught:
                await provider_with(lambda _r, s=status: httpx.Response(s)).extract(
                    extraction_request()
                )
            assert caught.value.retryable is True, f"status {status}"

    async def test_client_errors_are_terminal(self) -> None:
        with pytest.raises(ExtractionProviderError) as caught:
            await provider_with(lambda _r: httpx.Response(404)).extract(extraction_request())
        assert caught.value.retryable is False

    async def test_timeouts_are_retryable(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow model")

        with pytest.raises(ExtractionProviderError) as caught:
            await provider_with(handler).extract(extraction_request())
        assert caught.value.retryable is True
        assert "did not answer" in str(caught.value)


class TestOptionalProfile:
    def test_registration_is_explicit_and_carries_the_local_policy(self) -> None:
        register_local_llm_extraction(ENDPOINT, "test-model")
        try:
            info = provider_info(Capability.FIELD_EXTRACTION, PROVIDER_NAME)
            assert info.data_policy.processing_region == "local"
            assert info.data_policy.sends_content_to_third_party is False
        finally:
            unregister_provider(Capability.FIELD_EXTRACTION, PROVIDER_NAME)

    def test_an_unconfigured_deployment_has_no_local_llm_provider(self) -> None:
        from soa_worker.providers import UnknownProviderError

        with pytest.raises(UnknownProviderError):
            provider_info(Capability.FIELD_EXTRACTION, PROVIDER_NAME)


class TestBearerAuth:
    async def test_a_configured_key_travels_as_a_bearer_header(self) -> None:
        seen: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return model_answer(DEFAULT_FIELDS)

        provider = OpenAiCompatibleExtractionProvider(
            endpoint="https://api.openai.com/v1/chat/completions",
            model="gpt-4o",
            api_key="sk-openai-test",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        await provider.extract(extraction_request())
        (headers,) = seen
        assert headers["Authorization"] == "Bearer sk-openai-test"

    async def test_a_local_endpoint_sends_no_auth_header(self) -> None:
        seen: list[httpx.Headers] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers)
            return model_answer(DEFAULT_FIELDS)

        await provider_with(handler).extract(extraction_request())
        (headers,) = seen
        assert "authorization" not in headers


class TestHostedOpenAiProfile:
    def test_hosted_registration_names_the_provider_and_declares_third_party(self) -> None:
        register_hosted_openai_extraction(
            name="hosted-openai-compatible",
            endpoint="https://api.openai.com/v1/chat/completions",
            model="gpt-4o",
            api_key="sk-openai-test",
            region="us",
        )
        try:
            info = provider_info(Capability.FIELD_EXTRACTION, "hosted-openai-compatible")
            assert info.data_policy.sends_content_to_third_party is True
            assert info.data_policy.processing_region == "us"
        finally:
            unregister_provider(Capability.FIELD_EXTRACTION, "hosted-openai-compatible")

    def test_a_hosted_registration_without_a_key_is_refused(self) -> None:
        with pytest.raises(ValueError):
            register_hosted_openai_extraction(
                name="hosted-openai-compatible",
                endpoint="https://api.openai.com/v1/chat/completions",
                model="gpt-4o",
                api_key="",
                region="us",
            )
