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

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from soa_worker.extraction.provider import (
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
)
from soa_worker.model_extraction_result import (
    ModelOutputInvalidError,
    parse_model_extraction,
)
from soa_worker.model_request_builder import (
    BuildLimits,
    BuiltModelRequest,
    build_extraction_messages,
)

PROVIDER_NAME = "local-openai-compatible"

CAPABILITY_WARNING = (
    "local model extraction: confidence is the model's own uncalibrated "
    "self-report and evidence is page-level only — review gates apply"
)

_DETERMINISM_SEED = 7


class OpenAiCompatibleExtractionProvider:
    """See module docstring. ``client`` is injectable for tests; when
    omitted, a client with the configured timeout is created per call.
    ``instructions`` / ``instruction_reference`` are the stream's
    published AIO-010 content; ``redactor`` and ``build_limits`` flow
    into the AIO-011 injection-safe request builder."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        name: str = PROVIDER_NAME,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_tokens: int = 4000,
        client: httpx.AsyncClient | None = None,
        instructions: Mapping[str, Any] | None = None,
        instruction_reference: str | None = None,
        build_limits: BuildLimits | None = None,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._model = model
        # The registry verifies the instance reports its registered name;
        # a hosted OpenAI-compatible provider registers under its own
        # name (so operators can tell OpenAI from Gemini) and passes it
        # here.
        self._name = name
        # No key for a purely local endpoint (Ollama/vLLM/llama.cpp);
        # a key is required for the hosted OpenAI-compatible providers
        # (OpenAI, Gemini's OpenAI-compatible endpoint) and travels as a
        # Bearer token.
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._client = client
        self._instructions = instructions
        self._instruction_reference = instruction_reference
        self._build_limits = build_limits
        self._redactor = redactor

    @property
    def name(self) -> str:
        return self._name

    def build_payload(self, request: ExtractionRequest) -> tuple[dict[str, Any], BuiltModelRequest]:
        """The exact chat-completions payload, built via the AIO-011
        injection-safe builder — exposed so tests can inspect it.
        Carries no tools."""
        built = build_extraction_messages(
            request,
            instructions=self._instructions,
            instruction_reference=self._instruction_reference,
            limits=self._build_limits,
            redactor=self._redactor,
        )
        payload = {
            "model": self._model,
            "messages": list(built.messages),
            "temperature": 0,
            "seed": _DETERMINISM_SEED,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        return payload, built

    async def extract(
        self, request: ExtractionRequest, *, repair_hint: str | None = None
    ) -> ExtractionResult:
        """``repair_hint`` is the AIO-012 repair channel: a safe
        description of why the PREVIOUS response was invalid, appended
        as one extra user turn so the model can correct itself."""
        payload, built = self.build_payload(request)
        if repair_hint is not None:
            payload["messages"] = [
                *payload["messages"],
                {
                    "role": "user",
                    "content": (
                        f"Your previous response was invalid: {repair_hint}. Respond again "
                        "with ONLY the JSON object in the documented shape — no prose."
                    ),
                },
            ]
        assert "tools" not in payload  # the model reads; it never acts
        content = await self._complete(payload)
        return self._to_result(request, content, built)

    async def _complete(self, payload: dict[str, Any]) -> str:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            response = await client.post(self._endpoint, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ExtractionProviderError(
                f"the model did not answer within {self._timeout:.0f}s",
                retryable=True,
            ) from None
        except httpx.HTTPError:
            raise ExtractionProviderError(
                "the model endpoint could not be reached", retryable=True
            ) from None
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code >= 500 or response.status_code == 429:
            raise ExtractionProviderError(
                f"the model endpoint answered with status {response.status_code}",
                retryable=True,
            )
        if response.status_code >= 400:
            raise ExtractionProviderError(
                f"the model endpoint refused the request "
                f"(status {response.status_code}) — check the endpoint configuration",
                retryable=False,
            )
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ExtractionProviderError(
                "the model endpoint returned an unexpected response shape",
                retryable=True,
            ) from None
        if not isinstance(content, str):
            raise ExtractionProviderError("the model returned no text content", retryable=True)
        return content

    def _to_result(
        self, request: ExtractionRequest, content: str, built: BuiltModelRequest
    ) -> ExtractionResult:
        # Shared with the hosted adapters: same documented shape, same
        # honesty rules, one implementation (AIO-008). The capability
        # caveat leads the warnings.
        return parse_model_extraction(
            request,
            content,
            built,
            provider=self.name,
            model=self._model,
            cost_cents=0,
            lead_warnings=(CAPABILITY_WARNING,),
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


def register_hosted_openai_extraction(
    *,
    name: str,
    endpoint: str,
    model: str,
    api_key: str,
    region: str,
) -> None:
    """Register a HOSTED OpenAI-compatible provider (OpenAI, or Gemini's
    OpenAI-compatible endpoint) under its own ``name``. Unlike the local
    profile, this one sends content to a third party — the data policy
    says so honestly, keyed off the declared ``region``, so tenant policy
    is enforced against the truth. Call this ONLY when both a key and an
    endpoint are configured (worker startup does); a missing key means the
    provider simply does not exist (AIO-006 fail-closed)."""
    if not api_key:
        raise ValueError("a hosted OpenAI-compatible provider needs an API key")
    from soa_worker.providers.capabilities import (
        ANY_LANGUAGE,
        Capability,
        DataPolicy,
        ProviderInfo,
        register_provider,
    )

    register_provider(
        ProviderInfo(
            name=name,
            capability=Capability.FIELD_EXTRACTION,
            languages=(ANY_LANGUAGE,),
            data_policy=DataPolicy(
                processing_region=region,
                sends_content_to_third_party=True,
                retains_content=False,
                uses_content_for_training=False,
            ),
        ),
        lambda: OpenAiCompatibleExtractionProvider(
            endpoint=endpoint, model=model, name=name, api_key=api_key
        ),
    )


__all__ = [
    "CAPABILITY_WARNING",
    "PROVIDER_NAME",
    "ModelOutputInvalidError",
    "OpenAiCompatibleExtractionProvider",
    "register_hosted_openai_extraction",
    "register_local_llm_extraction",
]
