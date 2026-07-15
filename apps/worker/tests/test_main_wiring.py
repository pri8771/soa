"""Worker startup wiring tests: the pipeline's extraction provider is
selected from the AIO-001 registry by ``extraction_provider`` — the
deterministic mock by default, a registered real adapter when the
deployment names one, and a loud fail-closed startup error for a name
that is not registered (AIO-006)."""

import pytest
from pydantic import SecretStr

from soa_worker.anthropic_extraction import PROVIDER_NAME as ANTHROPIC_NAME
from soa_worker.llm_extraction import PROVIDER_NAME as LOCAL_NAME
from soa_worker.llm_extraction import (
    OpenAiCompatibleExtractionProvider,
    register_local_llm_extraction,
)
from soa_worker.main import _build_extraction_provider, _register_extraction_providers
from soa_worker.providers import (
    Capability,
    UnknownProviderError,
    registered_providers,
    unregister_provider,
)
from soa_worker.settings import Environment, WorkerSettings


def test_default_settings_select_the_mock_provider() -> None:
    provider = _build_extraction_provider(WorkerSettings(environment=Environment.TEST))
    assert provider.name == "mock"


def test_an_unregistered_provider_name_aborts_startup() -> None:
    settings = WorkerSettings(environment=Environment.TEST, extraction_provider="no-such-provider")
    with pytest.raises(UnknownProviderError):
        _build_extraction_provider(settings)


def test_a_registered_local_llm_provider_is_selected_by_name() -> None:
    register_local_llm_extraction("http://llm.local/v1/chat/completions", "test-model")
    try:
        settings = WorkerSettings(environment=Environment.TEST, extraction_provider=LOCAL_NAME)
        provider = _build_extraction_provider(settings)
        assert isinstance(provider, OpenAiCompatibleExtractionProvider)
        assert provider.name == LOCAL_NAME
    finally:
        unregister_provider(Capability.FIELD_EXTRACTION, LOCAL_NAME)


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
        extraction_provider=LOCAL_NAME,
    )
    _register_extraction_providers(settings)
    try:
        provider = _build_extraction_provider(settings)
        assert isinstance(provider, OpenAiCompatibleExtractionProvider)
        assert provider._timeout == 240.0
    finally:
        unregister_provider(Capability.FIELD_EXTRACTION, LOCAL_NAME)


def test_a_configured_anthropic_key_registers_the_claude_adapter() -> None:
    settings = WorkerSettings(
        environment=Environment.TEST, anthropic_api_key=SecretStr("sk-ant-test")
    )
    _register_extraction_providers(settings)
    try:
        names = {info.name for info in registered_providers(Capability.FIELD_EXTRACTION)}
        assert ANTHROPIC_NAME in names
    finally:
        unregister_provider(Capability.FIELD_EXTRACTION, ANTHROPIC_NAME)
