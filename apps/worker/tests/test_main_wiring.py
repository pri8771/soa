"""Worker startup registration tests for deployment-configured providers.

Production execution resolves the exact provider from each run's immutable
pins; startup only registers the capabilities a deployment can satisfy.
"""

from pydantic import SecretStr

from soa_worker.anthropic_extraction import PROVIDER_NAME as ANTHROPIC_NAME
from soa_worker.gemini_extraction import PROVIDER_NAME as GEMINI_NAME
from soa_worker.llm_extraction import PROVIDER_NAME as LOCAL_NAME
from soa_worker.llm_extraction import (
    OpenAiCompatibleExtractionProvider,
    register_local_llm_extraction,
)
from soa_worker.main import _register_extraction_providers
from soa_worker.providers import (
    Capability,
    create_provider,
    registered_providers,
    unregister_provider,
)
from soa_worker.settings import Environment, WorkerSettings


def _unregister_optional_providers() -> None:
    active = {info.name for info in registered_providers(Capability.FIELD_EXTRACTION)}
    for name in (LOCAL_NAME, ANTHROPIC_NAME, GEMINI_NAME):
        if name in active:
            unregister_provider(Capability.FIELD_EXTRACTION, name)


def test_a_registered_local_llm_provider_can_be_resolved_by_pinned_name() -> None:
    register_local_llm_extraction("http://llm.local/v1/chat/completions", "test-model")
    try:
        provider = create_provider(Capability.FIELD_EXTRACTION, LOCAL_NAME)
        assert isinstance(provider, OpenAiCompatibleExtractionProvider)
        assert provider.name == LOCAL_NAME
    finally:
        _unregister_optional_providers()


def test_the_default_local_llm_timeout_allows_minutes() -> None:
    # Real local extraction was observed at 45-250s; a short default
    # silently times out and retries the first real document. The default
    # must give local models on shared hardware room to finish.
    settings = WorkerSettings(environment=Environment.TEST)
    assert settings.local_llm_timeout_seconds == 300.0


def test_the_local_llm_timeout_setting_reaches_the_adapter() -> None:
    settings = WorkerSettings(
        environment=Environment.TEST,
        local_llm_endpoint="http://llm.local/v1/chat/completions",
        local_llm_timeout_seconds=240.0,
    )
    _register_extraction_providers(settings)
    try:
        provider = create_provider(Capability.FIELD_EXTRACTION, LOCAL_NAME)
        assert isinstance(provider, OpenAiCompatibleExtractionProvider)
        assert provider._timeout == 240.0
    finally:
        _unregister_optional_providers()


def test_a_configured_anthropic_key_registers_the_claude_adapter() -> None:
    settings = WorkerSettings(
        environment=Environment.TEST, anthropic_api_key=SecretStr("sk-ant-test")
    )
    _register_extraction_providers(settings)
    try:
        names = {info.name for info in registered_providers(Capability.FIELD_EXTRACTION)}
        assert ANTHROPIC_NAME in names
    finally:
        _unregister_optional_providers()
