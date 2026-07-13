"""Local OpenAI-compatible extraction adapter (AIO-007).

Speaks the OpenAI chat-completions protocol against a CONFIGURABLE
LOCAL endpoint (llama.cpp, vLLM, Ollama, ...) — an optional profile
that only registers when the deployment configures an endpoint (see
``register_local_llm_extraction`` and the worker settings). Content
stays inside the deployment, so the data policy is the strict local
one.

Request discipline:

- structured request: the schema fields and the page text travel as
  explicit JSON; the model is instructed to answer ONLY with JSON
  (``response_format: json_object``), values verbatim, ``null`` for
  anything not present;
- NO tool access: the request never carries a ``tools`` key — the
  model reads a document, it does not act (a test asserts this
  structurally);
- deterministic knobs pinned: ``temperature 0`` and a fixed ``seed``
  (honoured by engines that support it).

Honesty — the CLEAR MODEL CAPABILITY WARNING:

- confidence is the model's own self-report, uncalibrated; every
  result carries a warning saying so — downstream review gates treat
  it as one signal, never the decision;
- evidence is page-level only (full-page polygon plus the model's
  quote): a language model has no geometry, and inventing boxes would
  be fabrication. Quote-to-coordinate resolution is AIO-014's job. A
  page number the request does not contain yields NO evidence.
- a value for a field that was never requested is dropped with a
  warning, not passed along.

The prompt/document separation here is the basic one; the hardened
injection-safe request builder (data delimiters, bounded pages/tokens,
redaction hook) lands with AIO-010/011 and this adapter switches to it.
"""

import json
from typing import Any

import httpx

from soa_worker.extraction.provider import (
    EvidenceSpan,
    ExtractedField,
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
)

PROVIDER_NAME = "local-openai-compatible"

CAPABILITY_WARNING = (
    "local model extraction: confidence is the model's own uncalibrated "
    "self-report and evidence is page-level only — review gates apply"
)

_SYSTEM_PROMPT = (
    "You extract fields from business documents. Respond with ONE JSON object "
    'of the form {"fields": [{"key", "value", "confidence", "page_number", '
    '"quote", "row_index"}]}. Rules: values are copied VERBATIM from the '
    "document text; a field you cannot find gets value null; confidence is "
    "your own estimate between 0 and 1; page_number is the page the value "
    "appears on; quote is the exact surrounding text; row_index is the "
    "0-based row for table columns and null otherwise. Never invent values."
)

_DETERMINISM_SEED = 7


class OpenAiCompatibleExtractionProvider:
    """See module docstring. ``client`` is injectable for tests; when
    omitted, a client with the configured timeout is created per call."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_tokens: int = 4000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._model = model
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._client = client

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def build_payload(self, request: ExtractionRequest) -> dict[str, Any]:
        """The exact chat-completions payload — exposed so tests (and
        AIO-011's hardening) can inspect it. Carries no tools."""
        fields = [
            {
                "key": spec.key,
                "type": spec.field_type,
                "allowed_values": list(spec.enum_values) if spec.enum_values else None,
            }
            for spec in request.fields
        ]
        pages = [
            {"page_number": page.page_number, "text": page.text or ""}
            for page in sorted(request.pages, key=lambda p: p.page_number)
        ]
        user_message = json.dumps(
            {"fields_to_extract": fields, "document_pages": pages},
            ensure_ascii=False,
            sort_keys=True,
        )
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0,
            "seed": _DETERMINISM_SEED,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        payload = self.build_payload(request)
        assert "tools" not in payload  # the model reads; it never acts
        content = await self._complete(payload)
        return self._to_result(request, content)

    async def _complete(self, payload: dict[str, Any]) -> str:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            response = await client.post(self._endpoint, json=payload)
        except httpx.TimeoutException:
            raise ExtractionProviderError(
                f"the local model did not answer within {self._timeout:.0f}s",
                retryable=True,
            ) from None
        except httpx.HTTPError:
            raise ExtractionProviderError(
                "the local model endpoint could not be reached", retryable=True
            ) from None
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code >= 500 or response.status_code == 429:
            raise ExtractionProviderError(
                f"the local model endpoint answered with status {response.status_code}",
                retryable=True,
            )
        if response.status_code >= 400:
            raise ExtractionProviderError(
                f"the local model endpoint refused the request "
                f"(status {response.status_code}) — check the endpoint configuration",
                retryable=False,
            )
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ExtractionProviderError(
                "the local model endpoint returned an unexpected response shape",
                retryable=True,
            ) from None
        if not isinstance(content, str):
            raise ExtractionProviderError(
                "the local model returned no text content", retryable=True
            )
        return content

    def _to_result(self, request: ExtractionRequest, content: str) -> ExtractionResult:
        try:
            parsed = json.loads(content)
            entries = parsed["fields"]
            assert isinstance(entries, list)
        except (ValueError, KeyError, AssertionError, TypeError):
            # The content itself never goes into the error: it is model
            # output over customer data.
            raise ExtractionProviderError(
                "the local model returned JSON that does not match the "
                "expected shape; a bounded repair policy (AIO-012) may retry",
                retryable=True,
            ) from None

        requested = {spec.key for spec in request.fields}
        pages = {page.page_number: page for page in request.pages}
        warnings = [CAPABILITY_WARNING]
        found: dict[tuple[str, int | None], ExtractedField] = {}
        for entry in entries:
            if not isinstance(entry, dict) or "key" not in entry:
                continue
            key = str(entry["key"])
            if key not in requested:
                warnings.append(f"the model returned unrequested field {key!r}; dropped")
                continue
            value = entry.get("value")
            row_index = entry.get("row_index")
            row = int(row_index) if isinstance(row_index, int) and row_index >= 0 else None
            if value is None:
                continue  # absent: handled below with the requested sweep
            confidence = entry.get("confidence")
            numeric = float(confidence) if isinstance(confidence, int | float) else 0.5
            evidence: tuple[EvidenceSpan, ...] = ()
            page_number = entry.get("page_number")
            page = pages.get(page_number) if isinstance(page_number, int) else None
            if page is not None:
                quote = entry.get("quote")
                evidence = (
                    EvidenceSpan(
                        page_number=page.page_number,
                        polygon=(
                            (0.0, 0.0),
                            (float(page.width_px), 0.0),
                            (float(page.width_px), float(page.height_px)),
                            (0.0, float(page.height_px)),
                        ),
                        quote=str(quote) if isinstance(quote, str) and quote else None,
                    ),
                )
            found[(key, row)] = ExtractedField(
                field_key=key,
                raw_value=str(value),
                confidence=min(max(numeric, 0.0), 1.0),
                row_index=row,
                evidence=evidence,
            )

        results = list(found.values())
        answered_keys = {key for key, _ in found}
        for spec in request.fields:
            if spec.key not in answered_keys:
                results.append(ExtractedField(field_key=spec.key, raw_value=None, confidence=0.0))
        return ExtractionResult(
            provider=self.name,
            fields=tuple(results),
            model=self._model,
            cost_cents=0,
            warnings=tuple(warnings),
        )


def register_local_llm_extraction(endpoint: str, model: str) -> None:
    """Register the optional local profile. Call this ONLY when the
    deployment configured an endpoint (worker startup does); an
    unconfigured deployment simply has no such provider."""
    from soa_worker.providers.capabilities import (
        ANY_LANGUAGE,
        LOCAL_DATA_POLICY,
        Capability,
        ProviderInfo,
        register_provider,
    )

    register_provider(
        ProviderInfo(
            name=PROVIDER_NAME,
            capability=Capability.FIELD_EXTRACTION,
            languages=(ANY_LANGUAGE,),
            data_policy=LOCAL_DATA_POLICY,
        ),
        lambda: OpenAiCompatibleExtractionProvider(endpoint=endpoint, model=model),
    )


__all__ = [
    "CAPABILITY_WARNING",
    "PROVIDER_NAME",
    "OpenAiCompatibleExtractionProvider",
    "register_local_llm_extraction",
]
