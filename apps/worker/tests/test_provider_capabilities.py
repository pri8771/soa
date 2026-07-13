"""Capability registry tests (AIO-001): fail-closed resolution and
selection, strict data-policy defaults, and the built-in mock extraction
provider's registration."""

import pytest

from soa_worker.extraction.provider import ExtractionProvider
from soa_worker.providers import (
    ANY_LANGUAGE,
    LOCAL_DATA_POLICY,
    Capability,
    DataPolicy,
    NoCapableProviderError,
    ProviderInfo,
    UnknownProviderError,
    create_provider,
    provider_info,
    register_provider,
    registered_providers,
    require_provider,
    select_providers,
    unregister_provider,
)

EU_HOSTED = DataPolicy(
    processing_region="eu",
    sends_content_to_third_party=True,
    retains_content=False,
    uses_content_for_training=False,
)
US_RETAINING = DataPolicy(
    processing_region="us",
    sends_content_to_third_party=True,
    retains_content=True,
    uses_content_for_training=True,
)


def info(
    name: str,
    *,
    capability: Capability = Capability.OCR,
    languages: tuple[str, ...] = ("en",),
    data_policy: DataPolicy = LOCAL_DATA_POLICY,
) -> ProviderInfo:
    return ProviderInfo(
        name=name, capability=capability, languages=languages, data_policy=data_policy
    )


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture
def scratch_providers():
    """Register test providers and guarantee they are gone afterwards —
    the registry is process-global."""
    registered: list[tuple[Capability, str]] = []

    def add(provider_info_: ProviderInfo, factory=None) -> None:
        register_provider(provider_info_, factory or (lambda: _Named(provider_info_.name)))
        registered.append((provider_info_.capability, provider_info_.name))

    yield add
    for capability, name in registered:
        unregister_provider(capability, name)


class TestMetadataValidation:
    def test_data_policy_regions_are_lowercase_slugs(self) -> None:
        with pytest.raises(ValueError, match="lowercase"):
            DataPolicy("EU", True, False, False)

    def test_local_processing_cannot_claim_third_party(self) -> None:
        with pytest.raises(ValueError, match="third party"):
            DataPolicy("local", True, False, False)

    def test_wildcard_language_must_stand_alone(self) -> None:
        with pytest.raises(ValueError, match="only language"):
            info("x", languages=(ANY_LANGUAGE, "en"))

    def test_language_tags_are_lowercase(self) -> None:
        with pytest.raises(ValueError, match="lowercase"):
            info("x", languages=("EN",))

    def test_a_provider_declares_at_least_one_language(self) -> None:
        with pytest.raises(ValueError, match="at least one language"):
            info("x", languages=())


class TestRegistry:
    def test_resolution_fails_closed_for_unknown_providers(self) -> None:
        with pytest.raises(UnknownProviderError, match="nope"):
            provider_info(Capability.OCR, "nope")
        with pytest.raises(UnknownProviderError):
            create_provider(Capability.NATIVE_TEXT, "nope")

    def test_duplicate_registration_is_rejected(self, scratch_providers) -> None:
        scratch_providers(info("dup"))
        with pytest.raises(ValueError, match="already registered"):
            register_provider(info("dup"), lambda: _Named("dup"))

    def test_the_same_name_may_serve_different_capabilities(self, scratch_providers) -> None:
        scratch_providers(info("multi", capability=Capability.CLASSIFY))
        scratch_providers(info("multi", capability=Capability.SPLIT))
        assert provider_info(Capability.CLASSIFY, "multi").capability == Capability.CLASSIFY

    def test_unregistering_disables_the_capability_clearly(self, scratch_providers) -> None:
        register_provider(info("ephemeral"), lambda: _Named("ephemeral"))
        unregister_provider(Capability.OCR, "ephemeral")
        with pytest.raises(UnknownProviderError, match="ephemeral"):
            provider_info(Capability.OCR, "ephemeral")
        with pytest.raises(UnknownProviderError):
            unregister_provider(Capability.OCR, "ephemeral")

    def test_created_instances_must_report_their_registered_name(self, scratch_providers) -> None:
        scratch_providers(info("honest"))
        assert create_provider(Capability.OCR, "honest").name == "honest"
        scratch_providers(info("liar"), factory=lambda: _Named("someone-else"))
        with pytest.raises(ValueError, match="attributed to the wrong provider"):
            create_provider(Capability.OCR, "liar")


class TestSelection:
    def test_selection_matches_language_with_wildcard_support(self, scratch_providers) -> None:
        scratch_providers(info("english-only", languages=("en",)))
        scratch_providers(info("polyglot", languages=(ANY_LANGUAGE,)))
        names = [i.name for i in select_providers(Capability.OCR, language="de")]
        assert names == ["polyglot"]
        names = [i.name for i in select_providers(Capability.OCR, language="en")]
        assert names == ["english-only", "polyglot"]  # deterministic order

    def test_data_policy_defaults_are_strict(self, scratch_providers) -> None:
        scratch_providers(info("local-ocr"))
        scratch_providers(info("hosted-eu", data_policy=EU_HOSTED))
        scratch_providers(info("hosted-us", data_policy=US_RETAINING))
        # Default: nothing that ships content off-deployment is eligible.
        assert [i.name for i in select_providers(Capability.OCR)] == ["local-ocr"]
        # Explicitly allowing third-party processing admits the EU host,
        # but the retaining/training one still needs those allowed too.
        assert [
            i.name for i in select_providers(Capability.OCR, allow_third_party_processing=True)
        ] == ["hosted-eu", "local-ocr"]
        assert [
            i.name
            for i in select_providers(
                Capability.OCR,
                allow_third_party_processing=True,
                allow_content_retention=True,
                allow_training_on_content=True,
            )
        ] == ["hosted-eu", "hosted-us", "local-ocr"]

    def test_region_constraint(self, scratch_providers) -> None:
        scratch_providers(info("hosted-eu", data_policy=EU_HOSTED))
        pool = select_providers(Capability.OCR, region="eu", allow_third_party_processing=True)
        assert [i.name for i in pool] == ["hosted-eu"]
        assert select_providers(Capability.OCR, region="mars") == ()

    def test_require_provider_names_the_constraint_that_failed(self, scratch_providers) -> None:
        scratch_providers(info("english-only", languages=("en",)))
        assert require_provider(Capability.OCR, language="en").name == "english-only"
        with pytest.raises(NoCapableProviderError, match="language 'de'"):
            require_provider(Capability.OCR, language="de")

        scratch_providers(info("hosted-us", data_policy=US_RETAINING))
        with pytest.raises(NoCapableProviderError, match="data policy"):
            require_provider(Capability.OCR, language="en", region="us")

    def test_require_provider_says_when_nothing_is_registered_at_all(self) -> None:
        with pytest.raises(NoCapableProviderError, match="registered at all"):
            require_provider(Capability.NATIVE_TEXT)


class TestBuiltins:
    def test_the_mock_extraction_provider_is_registered_local(self) -> None:
        mock_info = provider_info(Capability.FIELD_EXTRACTION, "mock")
        assert mock_info.data_policy == LOCAL_DATA_POLICY
        assert mock_info.supports_language("de")
        instance = create_provider(Capability.FIELD_EXTRACTION, "mock")
        assert isinstance(instance, ExtractionProvider)

    def test_the_mock_is_selected_under_the_strictest_policy(self) -> None:
        assert "mock" in [i.name for i in select_providers(Capability.FIELD_EXTRACTION)]

    def test_listing_covers_all_capabilities(self) -> None:
        everything = registered_providers()
        assert any(i.capability == Capability.FIELD_EXTRACTION for i in everything)
