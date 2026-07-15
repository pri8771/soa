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
from soa_worker.model_usage import ProviderUsage, TokenPricing
from soa_worker.runtime_provenance import endpoint_fingerprint

PROVIDER_NAME = "local-openai-compatible"
DEFAULT_HOSTED_PRICING = TokenPricing(
    reference="soa-rate-card-v1:openai-compatible:gpt-4o",
    input_cents_per_million=250,
    output_cents_per_million=1000,
)

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

    supports_repair = True

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
        pricing: TokenPricing | None = None,
    ) -> None:
        if pricing is not None and not api_key:
            raise ValueError("a hosted OpenAI-compatible adapter needs an API key")
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
        self._pricing = pricing

    @property
    def name(self) -> str:
        return self._name

    @property
    def runtime_provenance(self) -> dict[str, object]:
        provenance: dict[str, object] = {
            "adapter": "openai-compatible-chat-completions-v1",
            "model": self._model,
            "endpoint_sha256": endpoint_fingerprint(self._endpoint),
        }
        if self._pricing is not None:
            provenance["pricing"] = self._pricing.as_dict()
        return provenance

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
        content, usage = await self._complete(payload)
        return self._to_result(request, content, built, usage)

    async def _complete(self, payload: dict[str, Any]) -> tuple[str, ProviderUsage | None]:
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
            raw_usage = body.get("usage")
            usage = (
                ProviderUsage.from_provider_counts(
                    input_tokens=raw_usage["prompt_tokens"],
                    output_tokens=raw_usage["completion_tokens"],
                    total_tokens=raw_usage["total_tokens"],
                )
                if raw_usage is not None
                else None
            )
        except (ValueError, KeyError, IndexError, TypeError):
            raise ExtractionProviderError(
                "the model endpoint returned an unexpected response shape",
                retryable=True,
            ) from None
        if not isinstance(content, str):
            raise ExtractionProviderError("the model returned no text content", retryable=True)
        if self._pricing is not None and usage is None:
            raise ExtractionProviderError(
                "the hosted model returned no token usage metadata", retryable=True
            )
        return content, usage

    def _to_result(
        self,
        request: ExtractionRequest,
        content: str,
        built: BuiltModelRequest,
        usage: ProviderUsage | None,
    ) -> ExtractionResult:
        # Shared with the hosted adapters: same documented shape, same
        # honesty rules, one implementation (AIO-008). The capability
        # caveat leads the warnings.
        pricing_reference = self._pricing.reference if self._pricing is not None else None
        return parse_model_extraction(
            request,
            content,
            built,
            provider=self.name,
            model=self._model,
            cost_cents=(
                self._pricing.estimate_cost_cents(usage)
                if self._pricing is not None and usage is not None
                else 0
            ),
            usage=usage,
            pricing_reference=pricing_reference,
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
        lambda **runtime: OpenAiCompatibleExtractionProvider(
            endpoint=endpoint,
            model=model,
            instructions=runtime.get("instructions"),
            instruction_reference=runtime.get("instruction_reference"),
        ),
    )


def register_hosted_openai_extraction(
    *,
    name: str,
    endpoint: str,
    model: str,
    api_key: str | None,
    region: str,
    pricing: TokenPricing = DEFAULT_HOSTED_PRICING,
) -> None:
    """Register a HOSTED OpenAI-compatible provider (OpenAI, or Gemini's
    OpenAI-compatible endpoint) under its own ``name``. Unlike the local
    profile, this one sends content to a third party — the data policy
    says so honestly, keyed off the declared ``region``, so tenant policy
    is enforced against the truth. The endpoint must be deployment-configured;
    the key may come from either deployment settings or the run's pinned tenant
    SecretReference. An explicitly empty deployment key remains invalid."""
    if api_key == "":
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
        lambda **runtime: OpenAiCompatibleExtractionProvider(
            endpoint=endpoint,
            model=model,
            name=name,
            api_key=(
                runtime["credential_value"] if "credential_value" in runtime else api_key or ""
            ),
            instructions=runtime.get("instructions"),
            instruction_reference=runtime.get("instruction_reference"),
            pricing=pricing,
        ),
    )


__all__ = [
    "CAPABILITY_WARNING",
    "DEFAULT_HOSTED_PRICING",
    "PROVIDER_NAME",
    "ModelOutputInvalidError",
    "OpenAiCompatibleExtractionProvider",
    "register_hosted_openai_extraction",
    "register_local_llm_extraction",
]
