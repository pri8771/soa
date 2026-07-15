"""Second hosted extraction adapter — Google Gemini (AIO-009).

The alternate hosted provider that proves the AIO-001 contract is
genuinely portable: a THIRD model API shape (Anthropic Messages in
AIO-008, OpenAI chat-completions in AIO-007's hosted mode, and now
Gemini's ``generateContent``) behind one interface, so the router
(AIO-013) can fall back across vendors without any pipeline change.

It reuses everything that keeps the model honest and safe:

- the AIO-011 injection-safe request builder — the built ``(system,
  user)`` pair maps onto Gemini's ``system_instruction`` field and one
  ``user`` content turn;
- the shared AIO-008 output parser — same documented JSON shape, same
  page-level evidence, same clamp/drop honesty, same repairable
  ``ModelOutputInvalidError``.

Gemini specifics:

- auth is an ``x-goog-api-key`` header (the key never rides in the URL,
  so it cannot leak through logs of the request line);
- ``responseMimeType: application/json`` asks for a bare JSON object and
  ``temperature 0`` pins determinism; NO tools are ever sent (a test
  asserts this);
- errors are display-safe and classified retryable/terminal exactly like
  the other adapters; a hosted provider declares third-party processing
  honestly and fails construction when its selected credential is absent
  or empty (AIO-006).
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

PROVIDER_NAME = "google-gemini"

DEFAULT_ENDPOINT_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.0-flash"
DEFAULT_PRICING = TokenPricing(
    reference="soa-rate-card-v1:google:gemini-2.0-flash",
    input_cents_per_million=10,
    output_cents_per_million=40,
)

CAPABILITY_WARNING = (
    "hosted Gemini extraction: content is sent to Google (third party); "
    "confidence is the model's own uncalibrated self-report and evidence is "
    "page-level only — review gates apply"
)


class GeminiExtractionProvider:
    """Gemini ``generateContent`` extraction. ``api_key`` is required (the
    adapter never registers without one). ``client`` is injectable for
    tests; when omitted a client with the configured timeout is created
    per call. ``endpoint_base`` is overridable for a proxy or a pinned API
    version."""

    supports_repair = True

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        endpoint_base: str = DEFAULT_ENDPOINT_BASE,
        timeout_seconds: float = 60.0,
        max_tokens: int = 4000,
        client: httpx.AsyncClient | None = None,
        instructions: dict[str, Any] | None = None,
        instruction_reference: str | None = None,
        build_limits: BuildLimits | None = None,
        pricing: TokenPricing = DEFAULT_PRICING,
    ) -> None:
        if not api_key:
            raise ValueError("the Gemini adapter needs an API key")
        self._api_key = api_key
        self._model = model
        self._endpoint = f"{endpoint_base}/{model}:generateContent"
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
            "adapter": "gemini-generate-content-v1beta",
            "model": self._model,
            "endpoint_sha256": endpoint_fingerprint(self._endpoint),
            "pricing": self._pricing.as_dict(),
        }

    def build_payload(self, request: ExtractionRequest) -> tuple[dict[str, Any], BuiltModelRequest]:
        """The exact ``generateContent`` payload — exposed for tests. The
        builder's system message becomes ``system_instruction``; its user
        message becomes one ``user`` content turn. Carries no tools."""
        built = build_extraction_messages(
            request,
            instructions=self._instructions,
            instruction_reference=self._instruction_reference,
            limits=self._build_limits,
        )
        system = next(m["content"] for m in built.messages if m["role"] == "system")
        user = next(m["content"] for m in built.messages if m["role"] == "user")
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self._max_tokens,
                "responseMimeType": "application/json",
            },
        }
        return payload, built

    async def extract(
        self, request: ExtractionRequest, *, repair_hint: str | None = None
    ) -> ExtractionResult:
        """``repair_hint`` is the AIO-012 repair channel: a safe reason the
        PREVIOUS response was invalid, appended as one extra user turn."""
        payload, built = self.build_payload(request)
        if repair_hint is not None:
            payload["contents"] = [
                *payload["contents"],
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                f"Your previous response was invalid: {repair_hint}. Respond "
                                "again with ONLY the JSON object in the documented shape, no prose."
                            )
                        }
                    ],
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
        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            response = await client.post(self._endpoint, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise ExtractionProviderError(
                f"Gemini did not answer within {self._timeout:.0f}s", retryable=True
            ) from None
        except httpx.HTTPError:
            raise ExtractionProviderError(
                "the Gemini endpoint could not be reached", retryable=True
            ) from None
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code >= 500 or response.status_code == 429:
            raise ExtractionProviderError(
                f"Gemini answered with status {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise ExtractionProviderError(
                f"Gemini refused the request (status {response.status_code}) — "
                "check the API key and model configuration",
                retryable=False,
            )
        try:
            body = response.json()
            parts = body["candidates"][0]["content"]["parts"]
            content = "".join(
                part["text"] for part in parts if isinstance(part, dict) and "text" in part
            )
            raw_usage = body["usageMetadata"]
            usage = ProviderUsage.from_provider_counts(
                input_tokens=raw_usage["promptTokenCount"],
                output_tokens=raw_usage["candidatesTokenCount"],
                total_tokens=raw_usage["totalTokenCount"],
            )
        except (ValueError, KeyError, IndexError, TypeError):
            raise ExtractionProviderError(
                "Gemini returned an unexpected response shape", retryable=True
            ) from None
        if not content:
            raise ExtractionProviderError("Gemini returned no text content", retryable=True)
        return content, usage


def register_gemini_extraction(
    api_key: str | None,
    *,
    model: str = DEFAULT_MODEL,
    endpoint_base: str = DEFAULT_ENDPOINT_BASE,
    pricing: TokenPricing = DEFAULT_PRICING,
) -> None:
    """Register hosted Gemini for deployment or run-pinned credentials.

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
                retains_content=False,
                uses_content_for_training=False,
            ),
        ),
        lambda **runtime: GeminiExtractionProvider(
            api_key=(
                runtime["credential_value"] if "credential_value" in runtime else api_key or ""
            ),
            model=model,
            endpoint_base=endpoint_base,
            instructions=runtime.get("instructions"),
            instruction_reference=runtime.get("instruction_reference"),
            pricing=pricing,
        ),
    )


__all__ = [
    "CAPABILITY_WARNING",
    "DEFAULT_ENDPOINT_BASE",
    "DEFAULT_MODEL",
    "DEFAULT_PRICING",
    "PROVIDER_NAME",
    "GeminiExtractionProvider",
    "register_gemini_extraction",
]
