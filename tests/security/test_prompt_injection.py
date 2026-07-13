"""Prompt-injection corpus tests (SEC-007).

REQUIRED in CI. Runs the shared injection corpus
(``soa_fixtures.injection_corpus``) through the two real defences:

1. the AIO-011 request builder must keep hostile DOCUMENT text from
   altering the system message or escaping the JSON data delimiter, and
   from ever turning tenant configuration into an exfiltration channel;
2. the AIO-007 response handling must stay SCHEMA-ONLY against hostile
   MODEL OUTPUT — unrequested keys dropped, injected tool calls ignored,
   values coerced to strings, confidence clamped, evidence confined to
   real pages.

Acceptance (SEC-007): extraction remains schema-only and performs no
prohibited action for any corpus entry.
"""

import json
import uuid
from typing import Any

import httpx
import pytest

from soa_fixtures.injection_corpus import DOCUMENT_INJECTIONS, MODEL_RESPONSE_ATTACKS
from soa_worker.extraction.provider import ExtractionRequest, FieldSpec, PageInput
from soa_worker.llm_extraction import OpenAiCompatibleExtractionProvider
from soa_worker.model_request_builder import (
    PLATFORM_SYSTEM_PROMPT,
    build_extraction_messages,
)

DOC_ID = uuid.UUID("5ec00007-0000-4000-8000-00000000ab1e")
ENDPOINT = "http://llm.local/v1/chat/completions"

FIELDS = (
    FieldSpec(key="po_number", field_type="text"),
    FieldSpec(key="currency", field_type="enum", enum_values=("EUR", "USD")),
)


def request_with_text(text: str) -> ExtractionRequest:
    return ExtractionRequest(
        document_id=DOC_ID,
        document_sha256="d" * 64,
        content_type="application/pdf",
        pages=(PageInput(page_number=1, width_px=1000, height_px=1400, text=text),),
        fields=FIELDS,
    )


# --- Document-content injections vs the request builder -----------------------


class TestDocumentInjectionsAgainstTheBuilder:
    @pytest.mark.parametrize("case", DOCUMENT_INJECTIONS, ids=lambda case: case.attack)
    def test_system_message_is_invariant_to_document_text(self, case: Any) -> None:
        benign = build_extraction_messages(request_with_text("PO-4711, currency EUR"))
        attacked = build_extraction_messages(request_with_text(case.text))
        system = attacked.messages[0]
        assert system["role"] == "system"
        # The system message never changes with the document, and the
        # platform core is present intact.
        assert system["content"] == benign.messages[0]["content"]
        assert system["content"] == PLATFORM_SYSTEM_PROMPT

    @pytest.mark.parametrize("case", DOCUMENT_INJECTIONS, ids=lambda case: case.attack)
    def test_document_text_stays_inside_the_json_data_delimiter(self, case: Any) -> None:
        built = build_extraction_messages(request_with_text(case.text))
        user = built.messages[1]
        assert user["role"] == "user"
        # The user message is valid JSON and the hostile text is confined
        # to a page's ``text`` string — it never became structure.
        payload = json.loads(user["content"])
        assert list(payload.keys()) == ["document_pages", "fields_to_extract"]
        pages = payload["document_pages"]
        assert len(pages) == 1
        # C0 control characters are stripped; the rest survives verbatim
        # as data (round-trips through JSON without escaping structure).
        assert "\x00" not in pages[0]["text"]
        assert "\x1b" not in pages[0]["text"]
        # No tool declarations leaked into the request regardless of text.
        assert "tools" not in payload
        assert all("tool" not in key for key in payload)


# --- Malicious model responses vs schema-only handling ------------------------


def provider_returning(content: str) -> OpenAiCompatibleExtractionProvider:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return OpenAiCompatibleExtractionProvider(
        endpoint=ENDPOINT,
        model="test-model",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestModelResponseAttacksStaySchemaOnly:
    @pytest.mark.parametrize("case", MODEL_RESPONSE_ATTACKS, ids=lambda case: case.attack)
    async def test_hostile_model_output_is_contained(self, case: Any) -> None:
        provider = provider_returning(case.text)
        request = request_with_text("PURCHASE ORDER\nPO-1\nCurrency: EUR")

        try:
            result = await provider.extract(request)
        except Exception as error:
            # Refusing to parse hostile output is an acceptable outcome;
            # crashing with an unclassified error is not.
            from soa_worker.extraction.provider import ExtractionProviderError

            assert isinstance(error, ExtractionProviderError)
            return

        requested = {spec.key for spec in FIELDS}
        # Only requested fields ever come back — no injected/unrequested
        # keys, no tool calls, nothing outside the schema.
        assert {field.field_key for field in result.fields} <= requested
        for field in result.fields:
            # Values are strings or absent — never nested structures a
            # model tried to smuggle in.
            assert field.raw_value is None or isinstance(field.raw_value, str)
            # Confidence is clamped to the unit interval.
            assert 0.0 <= field.confidence <= 1.0
            # Evidence only references pages that actually exist.
            for span in field.evidence:
                assert span.page_number in {page.page_number for page in request.pages}
        # The result object exposes no tool/side-effect channel.
        assert not hasattr(result, "tool_calls")
