import pytest

from soa_worker.model_usage import (
    ProviderCallUsage,
    ProviderUsage,
    TokenPricing,
    combine_provider_usage,
)


def test_provider_usage_retains_reported_total_independently() -> None:
    usage = ProviderUsage.from_provider_counts(input_tokens=100, output_tokens=20, total_tokens=150)
    assert usage.as_dict() == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 150,
    }


def test_provider_usage_derives_only_an_absent_total() -> None:
    assert (
        ProviderUsage.from_provider_counts(input_tokens=100, output_tokens=20).total_tokens == 120
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"input_tokens": True, "output_tokens": 0}, "input_tokens"),
        ({"input_tokens": -1, "output_tokens": 0}, "input_tokens"),
        ({"input_tokens": 1, "output_tokens": "2"}, "output_tokens"),
        ({"input_tokens": 10, "output_tokens": 20, "total_tokens": 5}, "total_tokens"),
    ],
)
def test_provider_usage_rejects_malformed_untrusted_counts(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ProviderUsage.from_provider_counts(**kwargs)


def test_pricing_estimate_combines_rates_then_rounds_up_once() -> None:
    pricing = TokenPricing(
        reference="rate-card:v1",
        input_cents_per_million=300,
        output_cents_per_million=1_500,
    )
    usage = ProviderUsage(input_tokens=2_000, output_tokens=1_000, total_tokens=3_000)
    assert pricing.estimate_cost_cents(usage) == 3
    assert pricing.as_dict()["reference"] == "rate-card:v1"


def test_zero_rate_stays_zero_and_invalid_rate_cards_are_rejected() -> None:
    usage = ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    assert TokenPricing("free:v1", 0, 0).estimate_cost_cents(usage) == 0
    with pytest.raises(ValueError, match="reference"):
        TokenPricing(" ", 1, 1)
    with pytest.raises(ValueError, match="input_cents"):
        TokenPricing("bad:v1", -1, 1)


def test_usage_combines_across_repair_attempts() -> None:
    first = ProviderUsage(10, 2, 12)
    second = ProviderUsage(20, 3, 25)
    assert combine_provider_usage(first, second) == ProviderUsage(30, 5, 37)
    assert combine_provider_usage(None, first) is first


def test_call_usage_exposes_reconcilable_billing_fields() -> None:
    record = ProviderCallUsage(
        provider="hosted",
        model="model-v1",
        usage=ProviderUsage(10, 2, 12),
        estimated_cost_cents=3,
        pricing_reference="rate:v1",
        outcome="invalid_output",
    )
    assert (record.billed_unit, record.billed_quantity) == ("tokens", 12)
    assert record.as_dict()["input_tokens"] == 10
    assert (
        ProviderCallUsage(
            provider="local",
            model=None,
            usage=None,
            estimated_cost_cents=0,
            pricing_reference=None,
            outcome="failed",
        ).billed_unit
        == "calls"
    )
