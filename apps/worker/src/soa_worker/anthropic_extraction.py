"""Hosted Claude extraction adapter (AIO-008).

The first HOSTED extraction adapter: it sends the document to Anthropic's
Messages API and returns fields in the platform's contract shape. It is
the one provider that needs its own module — Gemini and OpenAI both
expose an OpenAI-compatible ``/chat/completions`` endpoint and so reuse
the AIO-007 :class:`OpenAiCompatibleExtractionProvider` with just an
endpoint + key; Claude's Messages API is different enough (system as a
top-level field, ``x-api-key`` auth, an ``anthropic-version`` header, a
``content`` blocks response) to warrant this adapter.

What it REUSES so it cannot drift from the local adapter:

- the AIO-011 injection-safe request builder
  (:func:`build_extraction_messages`) — same system/document separation,
  same data delimiters, same no-tools/no-URLs discipline. The builder's
  ``(system, user)`` pair maps onto Messages' ``system`` field and a
  single ``user`` turn;
- the shared AIO-008 output parser (:func:`parse_model_extraction`) —
  same documented JSON shape, same honest evidence/confidence handling,
  same :class:`ModelOutputInvalidError` that the AIO-012 repair policy
  drives.

Honesty and safety, same as every model adapter:

- NO tools are ever sent (a test asserts the request body carries none) —
  the model reads a document, it never acts;
- ``temperature 0`` for determinism (Anthropic exposes no seed);
- evidence is page-level only; confidence is the model's own self-report;
- errors are display-safe (no vendor payloads, no document content) and
  classified retryable/terminal exactly like the local adapter.

Data policy: this adapter sends content to a THIRD PARTY (hosted, US
region). It declares that honestly in its registration
(:func:`register_anthropic_extraction`) so tenant retention/provider
policy is enforced against the truth — a hosted adapter is only ever
eligible when the resolved policy explicitly allows third-party
processing (see ``select_providers``). Registration supports either a
deployment key or a run-pinned tenant secret; construction fails closed
when the selected credential is missing or empty (AIO-006).
"""

from typing import Any

import httpx

from soa_worker.extraction.provider import (
    ExtractionProviderError,
    ExtractionRequest,
    ExtractionResult,
)
from soa_worker.model_extraction_result import parse_model_extraction
from soa_worker.model_request_builder import (
    BuildLimits,
    BuiltModelRequest,
    build_extraction_messages,
)
from soa_worker.model_usage import ProviderUsage, TokenPricing
from soa_worker.runtime_provenance import endpoint_fingerprint

PROVIDER_NAME = "anthropic-claude"

#: Default hosted endpoint and API version. Both are overridable so a
#: proxy or a pinned version can be configured without a code change.
DEFAULT_ENDPOINT = "https://api.anthropic.com/v1/messages"
DEFAULT_API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-5"
DEFAULT_PRICING = TokenPricing(
    reference="soa-rate-card-v1:anthropic:claude-sonnet-4-5",
    input_cents_per_million=300,
    output_cents_per_million=1500,
)

CAPABILITY_WARNING = (
    "hosted Claude extraction: content is sent to Anthropic (third party); "
    "confidence is the model's own uncalibrated self-report and evidence is "
    "page-level only — review gates apply"
)


class AnthropicExtractionProvider:
    """Claude Messages-API extraction. ``api_key`` is required (the
    adapter never registers without one). ``client`` is injectable for
    tests; when omitted a client with the configured timeout is created
    per call. Builder inputs (``instructions``/``build_limits``/…) flow
    through unchanged, so the request is built by the SAME AIO-011
    injection-safe path the local adapter uses."""

    supports_repair = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        endpoint: str = DEFAULT_ENDPOINT,
        api_version: str = DEFAULT_API_VERSION,
        timeout_seconds: float = 60.0,
        max_tokens: int = 4000,
        client: httpx.AsyncClient | None = None,
        instructions: dict[str, Any] | None = None,
        instruction_reference: str | None = None,
        build_limits: BuildLimits | None = None,
        pricing: TokenPricing = DEFAULT_PRICING,
    ) -> None:
        if not api_key:
            raise ValueError("the Claude adapter needs an API key")
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint
        self._api_version = api_version
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._client = client
        self._instructions = instructions
        self._instruction_reference = instruction_reference
        self._build_limits = build_limits
        self._pricing = pricing

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def runtime_provenance(self) -> dict[str, object]:
        return {
            "adapter": "anthropic-messages-v1",
            "api_version": self._api_version,
            "model": self._model,
            "endpoint_sha256": endpoint_fingerprint(self._endpoint),
            "pricing": self._pricing.as_dict(),
        }

    def build_payload(self, request: ExtractionRequest) -> tuple[dict[str, Any], BuiltModelRequest]:
        """The exact Messages-API payload — exposed for tests. The
        AIO-011 builder's system message becomes the top-level ``system``
        field; its user message becomes the single ``user`` turn. Carries
        no tools."""
        built = build_extraction_messages(
            request,
            instructions=self._instructions,
            instruction_reference=self._instruction_reference,
            limits=self._build_limits,
        )
        system = next(m["content"] for m in built.messages if m["role"] == "system")
        user = next(m["content"] for m in built.messages if m["role"] == "user")
        payload = {
            "model": self._model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": self._max_tokens,
        }
        return payload, built

    async def extract(
        self, request: ExtractionRequest, *, repair_hint: str | None = None
    ) -> ExtractionResult:
        """``repair_hint`` is the AIO-012 repair channel: a safe reason
        the PREVIOUS response was invalid, appended as one extra user
        turn so the model can correct itself."""
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
        return parse_model_extraction(
            request,
            content,
            built,
            provider=self.name,
            model=self._model,
            cost_cents=self._pricing.estimate_cost_cents(usage),
            usage=usage,
            pricing_reference=self._pricing.reference,
            lead_warnings=(CAPABILITY_WARNING,),
        )

    async def _complete(self, payload: dict[str, Any]) -> tuple[str, ProviderUsage]:
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._api_version,
            "content-type": "application/json",
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            response = await client.post(self._endpoint, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ExtractionProviderError(
                f"Claude did not answer within {self._timeout:.0f}s", retryable=True
            ) from None
        except httpx.HTTPError:
            raise ExtractionProviderError(
                "the Claude endpoint could not be reached", retryable=True
            ) from None
        finally:
            if owns_client:
                await client.aclose()
        # 429 and 5xx are transient; 401/403 (bad/again-revoked key) and
        # other 4xx are terminal — retrying a rejected key just wastes it.
        if response.status_code >= 500 or response.status_code == 429:
            raise ExtractionProviderError(
                f"Claude answered with status {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise ExtractionProviderError(
                f"Claude refused the request (status {response.status_code}) — "
                "check the API key and model configuration",
                retryable=False,
            )
        try:
            body = response.json()
            blocks = body["content"]
            content = "".join(
                block["text"]
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            raw_usage = body["usage"]
            usage = ProviderUsage.from_provider_counts(
                input_tokens=raw_usage["input_tokens"],
                output_tokens=raw_usage["output_tokens"],
            )
        except (ValueError, KeyError, IndexError, TypeError):
            raise ExtractionProviderError(
                "Claude returned an unexpected response shape", retryable=True
            ) from None
        if not content:
            raise ExtractionProviderError("Claude returned no text content", retryable=True)
        return content, usage


def register_anthropic_extraction(
    api_key: str | None,
    *,
    model: str = DEFAULT_MODEL,
    endpoint: str = DEFAULT_ENDPOINT,
    pricing: TokenPricing = DEFAULT_PRICING,
) -> None:
    """Register hosted Claude for deployment or run-pinned credentials.

    A supplied tenant ``credential_value`` is authoritative, including an
    invalid empty value; it never silently falls through to shared billing.
    With neither credential source, construction fails closed (AIO-006).
    """
    from soa_worker.providers.capabilities import (
        ANY_LANGUAGE,
        Capability,
        DataPolicy,
        ProviderInfo,
        register_provider,
    )

    register_provider(
        ProviderInfo(
            name=PROVIDER_NAME,
            capability=Capability.FIELD_EXTRACTION,
            languages=(ANY_LANGUAGE,),
            data_policy=DataPolicy(
                processing_region="us",
                sends_content_to_third_party=True,
                # Anthropic's API does not retain inputs for training by
                # default; the deployment's contract terms are the source
                # of truth and are documented before production use
                # (AIO-008 acceptance). Declared conservatively here.
                retains_content=False,
                uses_content_for_training=False,
            ),
        ),
        lambda **runtime: AnthropicExtractionProvider(
            api_key=(
                runtime["credential_value"] if "credential_value" in runtime else api_key or ""
            ),
            model=model,
            endpoint=endpoint,
            instructions=runtime.get("instructions"),
            instruction_reference=runtime.get("instruction_reference"),
            pricing=pricing,
        ),
    )


__all__ = [
    "CAPABILITY_WARNING",
    "DEFAULT_API_VERSION",
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "DEFAULT_PRICING",
    "PROVIDER_NAME",
    "AnthropicExtractionProvider",
    "register_anthropic_extraction",
]
