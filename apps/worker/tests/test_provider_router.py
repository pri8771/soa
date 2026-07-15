"""Provider router tests (AIO-013): the policy matrix — privacy,
local-only, language, region, budget, quality, health, preference — the
deterministic explanation, and the native/scanned fork."""

import pytest

from soa_worker.provider_router import (
    BudgetExceededError,
    DocumentFacts,
    NoRouteError,
    OperationalSignals,
    RoutingPolicy,
    route,
    route_text_capability,
)
from soa_worker.providers.capabilities import (
    ANY_LANGUAGE,
    LOCAL_DATA_POLICY,
    Capability,
    DataPolicy,
    ProviderInfo,
    register_provider,
    unregister_provider,
)

CAP = Capability.CLASSIFY  # no built-in adapters occupy this pool

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

POOL = (
    ProviderInfo(
        name="local-fast", capability=CAP, languages=("en", "de"), data_policy=LOCAL_DATA_POLICY
    ),
    ProviderInfo(
        name="hosted-eu", capability=CAP, languages=(ANY_LANGUAGE,), data_policy=EU_HOSTED
    ),
    ProviderInfo(
        name="hosted-us", capability=CAP, languages=(ANY_LANGUAGE,), data_policy=US_RETAINING
    ),
)

SIGNALS = OperationalSignals(
    quality={"local-fast": 0.7, "hosted-eu": 0.9, "hosted-us": 0.8},
    cost_cents={"hosted-eu": 10, "hosted-us": 2},
)

ALLOW_ALL = RoutingPolicy(
    allow_third_party_processing=True,
    allow_content_retention=True,
    allow_training_on_content=True,
)


@pytest.fixture(autouse=True)
def pool():
    for info in POOL:
        register_provider(info, lambda name=info.name: name)
    yield
    for info in POOL:
        unregister_provider(CAP, info.name)


class TestPrivacyAndPolicy:
    def test_the_strict_default_admits_only_local_providers(self) -> None:
        decision = route(CAP, policy=RoutingPolicy(), signals=SIGNALS)
        assert decision.provider.name == "local-fast"
        assert decision.fallbacks == ()

    def test_local_only_is_honoured_over_preference_and_quality(self) -> None:
        policy = RoutingPolicy(
            local_only=True,
            allow_third_party_processing=True,  # local_only overrides even this
            preferred_order=("hosted-eu",),
        )
        decision = route(CAP, policy=policy, signals=SIGNALS)
        assert decision.provider.name == "local-fast"
        assert any("local-only" in line for line in decision.explanation)

    def test_regions_constrain_hosted_but_never_local(self) -> None:
        policy = RoutingPolicy(
            allowed_regions=("eu",),
            allow_third_party_processing=True,
            allow_content_retention=True,
            allow_training_on_content=True,
        )
        decision = route(CAP, policy=policy, signals=SIGNALS)
        assert decision.provider.name == "hosted-eu"  # best quality in-region
        assert "local-fast" in decision.fallbacks
        assert any(
            "eliminated hosted-us" in line and "region" in line for line in decision.explanation
        )

    def test_language_narrows_the_pool(self) -> None:
        decision = route(CAP, policy=ALLOW_ALL, facts=DocumentFacts(language="fr"), signals=SIGNALS)
        # local-fast declares only en/de; the wildcard hosts survive.
        assert decision.provider.name == "hosted-eu"
        assert decision.fallbacks == ("hosted-us",)
        assert "local-fast" not in decision.explanation[0]
        assert "language 'fr'" in decision.explanation[0]

    def test_pinned_chain_excludes_unconfigured_eligible_adapters(self) -> None:
        policy = RoutingPolicy(
            allow_third_party_processing=True,
            allow_content_retention=True,
            allow_training_on_content=True,
            allowed_providers=("local-fast", "hosted-us"),
            preferred_order=("hosted-us", "local-fast"),
        )
        decision = route(CAP, policy=policy, signals=SIGNALS)
        assert decision.provider.name == "hosted-us"
        assert decision.fallbacks == ("local-fast",)
        assert any("pinned provider chain" in line for line in decision.explanation)


class TestBudget:
    def test_over_budget_candidates_are_skipped_with_the_reason(self) -> None:
        policy = RoutingPolicy(
            allow_third_party_processing=True,
            allow_content_retention=True,
            allow_training_on_content=True,
            budget_cents=5,
        )
        decision = route(CAP, policy=policy, signals=SIGNALS)
        assert decision.provider.name == "hosted-us"  # eu (10c) skipped, us (2c) fits
        assert decision.estimated_cost_cents == 2
        assert any(
            "skipped hosted-eu" in line and "exceeds" in line for line in decision.explanation
        )

    def test_the_budget_is_never_exceeded_silently(self) -> None:
        register_provider(
            ProviderInfo(
                name="only-hosted",
                capability=Capability.SPLIT,
                languages=(ANY_LANGUAGE,),
                data_policy=EU_HOSTED,
            ),
            lambda: "only-hosted",
        )
        try:
            policy = RoutingPolicy(allow_third_party_processing=True, budget_cents=1)
            with pytest.raises(BudgetExceededError, match="exceed the 1 cent budget"):
                route(
                    Capability.SPLIT,
                    policy=policy,
                    signals=OperationalSignals(cost_cents={"only-hosted": 9}),
                )
        finally:
            unregister_provider(Capability.SPLIT, "only-hosted")

    def test_an_unknown_hosted_cost_cannot_be_spent_when_a_budget_applies(self) -> None:
        policy = RoutingPolicy(
            allow_third_party_processing=True,
            allow_content_retention=True,
            allow_training_on_content=True,
            budget_cents=100,
        )
        no_costs = OperationalSignals(quality=SIGNALS.quality)  # no estimates at all
        decision = route(CAP, policy=policy, signals=no_costs)
        assert decision.provider.name == "local-fast"  # local is free; hosted unknown
        assert any("unknown cost" in line for line in decision.explanation)


class TestQualityHealthAndPreference:
    def test_quality_ranks_the_candidates(self) -> None:
        decision = route(CAP, policy=ALLOW_ALL, signals=SIGNALS)
        assert decision.provider.name == "hosted-eu"  # 0.9 beats 0.8 and 0.7
        assert decision.fallbacks == ("hosted-us", "local-fast")

    def test_the_quality_floor_eliminates_scored_providers_below_it(self) -> None:
        policy = RoutingPolicy(
            allow_third_party_processing=True,
            allow_content_retention=True,
            allow_training_on_content=True,
            evaluated_quality_scores=SIGNALS.quality,
            min_quality=0.75,
        )
        decision = route(CAP, policy=policy, signals=SIGNALS)
        assert decision.provider.name == "hosted-eu"
        assert any(
            "eliminated local-fast" in line and "below" in line for line in decision.explanation
        )

    def test_a_quality_floor_fails_closed_for_unknown_scores(self) -> None:
        with pytest.raises(NoRouteError) as caught:
            route(
                CAP,
                policy=RoutingPolicy(min_quality=0.8),
                signals=OperationalSignals(),
            )
        assert any("evaluated quality is not pinned" in line for line in caught.value.explanation)

    def test_unreachable_providers_are_eliminated(self) -> None:
        signals = OperationalSignals(
            quality=SIGNALS.quality,
            cost_cents=SIGNALS.cost_cents,
            health={"hosted-eu": "unreachable"},
        )
        decision = route(CAP, policy=ALLOW_ALL, signals=signals)
        assert decision.provider.name == "hosted-us"
        assert any("unreachable" in line for line in decision.explanation)

    def test_degraded_providers_rank_below_healthy_ones(self) -> None:
        signals = OperationalSignals(
            quality=SIGNALS.quality,
            cost_cents=SIGNALS.cost_cents,
            health={"hosted-eu": "degraded"},
        )
        decision = route(CAP, policy=ALLOW_ALL, signals=signals)
        assert decision.provider.name == "hosted-us"  # healthy 0.8 beats degraded 0.9
        # Degraded ranks below every healthy candidate but stays available.
        assert decision.fallbacks == ("local-fast", "hosted-eu")

    def test_the_preferred_order_is_the_fallback_order(self) -> None:
        policy = RoutingPolicy(
            allow_third_party_processing=True,
            allow_content_retention=True,
            allow_training_on_content=True,
            preferred_order=("hosted-us", "local-fast"),
        )
        decision = route(CAP, policy=policy, signals=SIGNALS)
        assert decision.provider.name == "hosted-us"  # preference beats quality
        assert decision.fallbacks == ("local-fast", "hosted-eu")


class TestExplanationAndFailure:
    def test_the_decision_is_deterministic_including_its_explanation(self) -> None:
        first = route(CAP, policy=ALLOW_ALL, signals=SIGNALS)
        second = route(CAP, policy=ALLOW_ALL, signals=SIGNALS)
        assert first == second
        assert first.explanation[-1].startswith("selected hosted-eu")

    def test_no_route_is_an_explicit_error_with_the_full_story(self) -> None:
        with pytest.raises(NoRouteError) as caught:
            route(
                CAP,
                policy=RoutingPolicy(local_only=True),
                facts=DocumentFacts(language="fr"),  # local-fast lacks fr
                signals=SIGNALS,
            )
        assert any("candidates" in line for line in caught.value.explanation)


class TestNativeScannedFork:
    def test_good_native_coverage_routes_to_native_text(self) -> None:
        capability, reason = route_text_capability(DocumentFacts(native_text_coverage=0.95))
        assert capability is Capability.NATIVE_TEXT
        assert "digital document" in reason

    def test_poor_coverage_routes_to_ocr(self) -> None:
        capability, reason = route_text_capability(DocumentFacts(native_text_coverage=0.1))
        assert capability is Capability.OCR
        assert "10%" in reason

    def test_missing_native_text_routes_to_ocr(self) -> None:
        capability, reason = route_text_capability(DocumentFacts())
        assert capability is Capability.OCR
        assert "not available" in reason
