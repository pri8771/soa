from soa_worker.llm_extraction import OpenAiCompatibleExtractionProvider
from soa_worker.runtime_provenance import (
    endpoint_fingerprint,
    extraction_provenance,
    renderer_provenance,
    runtime_fingerprint,
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
    assert len(str(provenance["endpoint_sha256"])) == 64
    assert "internal.example" not in str(provenance)
    assert "credential-value" not in str(provenance)
    assert "secret" not in str(provenance)


def test_renderer_provenance_names_actual_engine_family() -> None:
    assert renderer_provenance("application/pdf")["engine"]["name"] == "pypdfium2"
    assert renderer_provenance("image/png")["engine"]["name"] == "Pillow"
