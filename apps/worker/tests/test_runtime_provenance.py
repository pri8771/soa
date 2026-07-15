import pytest

from soa_worker.anthropic_extraction import AnthropicExtractionProvider
from soa_worker.gemini_extraction import GeminiExtractionProvider
from soa_worker.llm_extraction import OpenAiCompatibleExtractionProvider
from soa_worker.runtime_provenance import (
    endpoint_fingerprint,
    extraction_provenance,
    renderer_provenance,
    runtime_fingerprint,
    safe_adapter_provenance,
)


def test_endpoint_fingerprint_drops_credentials_query_and_fragment() -> None:
    left = endpoint_fingerprint(
        "https://user:secret@Models.EXAMPLE.com/v1/chat/completions?api_key=never-store#x"
    )
    right = endpoint_fingerprint("https://models.example.com/v1/chat/completions")
    assert left == right
    assert "secret" not in left
    assert len(left) == 64


def test_runtime_fingerprint_is_order_independent_and_changes_with_model() -> None:
    first = {"model": "m1", "provider": "p", "nested": {"b": 2, "a": 1}}
    reordered = {"nested": {"a": 1, "b": 2}, "provider": "p", "model": "m1"}
    assert runtime_fingerprint(first) == runtime_fingerprint(reordered)
    assert runtime_fingerprint(first) != runtime_fingerprint(first | {"model": "m2"})


def test_extraction_provenance_records_model_and_only_endpoint_digest() -> None:
    provider = OpenAiCompatibleExtractionProvider(
        endpoint="https://internal.example/v1/chat?token=secret",
        model="model-2026-07",
        api_key="credential-value",
    )
    provenance = extraction_provenance(
        provider,
        name=provider.name,
        model="model-2026-07",
    )
    assert provenance["model"] == "model-2026-07"
    assert provenance["timeout_seconds"] == 60.0
    assert provenance["max_tokens"] == 4000
    assert provenance["seed"] == 7
    assert len(str(provenance["endpoint_sha256"])) == 64
    assert "internal.example" not in str(provenance)
    assert "credential-value" not in str(provenance)
    assert "secret" not in str(provenance)


@pytest.mark.parametrize(
    ("provider", "seeded"),
    [
        (
            OpenAiCompatibleExtractionProvider(
                endpoint="https://openai.example/v1/chat/completions?key=secret",
                model="openai-test",
                api_key="not-persisted",
                timeout_seconds=12.5,
                max_tokens=321,
            ),
            True,
        ),
        (
            AnthropicExtractionProvider(
                api_key="not-persisted",
                endpoint="https://anthropic.example/v1/messages?key=secret",
                model="anthropic-test",
                timeout_seconds=12.5,
                max_tokens=321,
            ),
            False,
        ),
        (
            GeminiExtractionProvider(
                api_key="not-persisted",
                endpoint_base="https://gemini.example/v1beta/models?key=secret",
                model="gemini-test",
                timeout_seconds=12.5,
                max_tokens=321,
            ),
            False,
        ),
    ],
)
def test_model_adapters_report_the_same_safe_runtime_knobs(
    provider: (
        OpenAiCompatibleExtractionProvider | AnthropicExtractionProvider | GeminiExtractionProvider
    ),
    seeded: bool,
) -> None:
    provenance = provider.runtime_provenance
    assert provenance["timeout_seconds"] == 12.5
    assert provenance["max_tokens"] == 321
    assert provenance["temperature"] == 0
    assert len(str(provenance["endpoint_sha256"])) == 64
    assert ("seed" in provenance) is seeded
    serialized = str(provenance)
    assert "not-persisted" not in serialized
    assert "secret" not in serialized


def test_adapter_provenance_drops_unknown_and_unhashed_endpoint_fields() -> None:
    class ExtensionAdapter:
        @property
        def runtime_provenance(self) -> dict[str, object]:
            return {
                "adapter": "extension-v1",
                "model": "model-v1",
                "api_key": "must-not-persist",
                "endpoint_sha256": "https://endpoint.example?credential=must-not-persist",
                "unknown": {"secret": "must-not-persist"},
            }

    assert safe_adapter_provenance(ExtensionAdapter()) == {
        "adapter": "extension-v1",
        "model": "model-v1",
    }


def test_renderer_provenance_names_actual_engine_family() -> None:
    assert renderer_provenance("application/pdf")["engine"]["name"] == "pypdfium2"
    assert renderer_provenance("image/png")["engine"]["name"] == "Pillow"
