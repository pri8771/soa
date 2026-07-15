"""Typed provider token usage and pinned cost estimation.

Provider responses report token counts; those are facts.  A deployment's
rate card turns the input/output facts into an integer-cent estimate.  The
two intentionally remain separate so a later invoice reconciliation can
correct the estimate without rewriting what the provider reported.

Rates are explicit integer cents per one million tokens.  No adapter fetches
pricing from a vendor or assumes that a public price is still current.  The
rate-card reference and exact rates travel in runtime provenance.
"""

from dataclasses import dataclass
from typing import Any, Literal

TOKENS_PER_MILLION = 1_000_000


def _token_count(value: Any, *, field: str) -> int:
    """Accept a provider token count without Python's bool-as-int trap."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class ProviderUsage:
    """Token facts returned for one provider call.

    ``total_tokens`` is retained independently from the input/output
    breakdown because some providers include additional metered token classes
    in their total.  It therefore only has to be at least each component; it
    need not equal their sum.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int

    def __post_init__(self) -> None:
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            _token_count(getattr(self, field), field=field)
        if self.total_tokens < max(self.input_tokens, self.output_tokens):
            raise ValueError("total_tokens cannot be smaller than a token component")

    @classmethod
    def from_provider_counts(
        cls,
        *,
        input_tokens: Any,
        output_tokens: Any,
        total_tokens: Any | None = None,
    ) -> "ProviderUsage":
        """Validate untrusted response fields and derive only a missing total.

        Anthropic reports input and output counts but no total; summing those
        two facts is the only derivation performed here.  Providers that do
        report a total keep their exact value.
        """
        parsed_input = _token_count(input_tokens, field="input_tokens")
        parsed_output = _token_count(output_tokens, field="output_tokens")
        parsed_total = (
            parsed_input + parsed_output
            if total_tokens is None
            else _token_count(total_tokens, field="total_tokens")
        )
        return cls(
            input_tokens=parsed_input,
            output_tokens=parsed_output,
            total_tokens=parsed_total,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class TokenPricing:
    """A versioned deployment rate card, never a live vendor lookup."""

    reference: str
    input_cents_per_million: int
    output_cents_per_million: int

    def __post_init__(self) -> None:
        if not self.reference.strip() or len(self.reference) > 200:
            raise ValueError("pricing reference must be 1..200 characters")
        for field in ("input_cents_per_million", "output_cents_per_million"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")

    def estimate_cost_cents(self, usage: ProviderUsage) -> int:
        """Return a conservative whole-cent estimate for one call.

        The ledger stores integer cents, so any non-zero fractional-cent
        aggregate rounds upward once after input and output are combined.
        This prevents small paid calls from disappearing as zero-cost usage.
        """
        numerator = (
            usage.input_tokens * self.input_cents_per_million
            + usage.output_tokens * self.output_cents_per_million
        )
        if numerator == 0:
            return 0
        return (numerator + TOKENS_PER_MILLION - 1) // TOKENS_PER_MILLION

    def as_dict(self) -> dict[str, str | int]:
        return {
            "reference": self.reference,
            "input_cents_per_million": self.input_cents_per_million,
            "output_cents_per_million": self.output_cents_per_million,
        }


@dataclass(frozen=True)
class ProviderCallUsage:
    """One provider call's reconcilable facts and pinned estimate.

    Keeping calls separate lets a routed fallback attribute tokens and cost to
    the provider that actually incurred them instead of manufacturing a
    composite row under the eventual winner.
    """

    provider: str
    model: str | None
    usage: ProviderUsage | None
    estimated_cost_cents: int
    pricing_reference: str | None
    outcome: Literal["succeeded", "invalid_output", "failed", "manual_review"]

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider usage record needs a provider")
        if (
            isinstance(self.estimated_cost_cents, bool)
            or not isinstance(self.estimated_cost_cents, int)
            or self.estimated_cost_cents < 0
        ):
            raise ValueError("estimated_cost_cents must be a non-negative integer")

    @property
    def billed_unit(self) -> str:
        return "tokens" if self.usage is not None else "calls"

    @property
    def billed_quantity(self) -> int:
        return self.usage.total_tokens if self.usage is not None else 1

    def as_dict(self) -> dict[str, str | int | None]:
        token_facts = self.usage.as_dict() if self.usage is not None else {}
        return {
            "provider": self.provider,
            "model": self.model,
            "estimated_cost_cents": self.estimated_cost_cents,
            "pricing_reference": self.pricing_reference,
            "outcome": self.outcome,
            "input_tokens": token_facts.get("input_tokens"),
            "output_tokens": token_facts.get("output_tokens"),
            "total_tokens": token_facts.get("total_tokens"),
        }


def combine_provider_usage(
    first: ProviderUsage | None, second: ProviderUsage | None
) -> ProviderUsage | None:
    """Sum known call facts across bounded model-repair attempts."""
    if first is None:
        return second
    if second is None:
        return first
    return ProviderUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
    )


__all__ = [
    "TOKENS_PER_MILLION",
    "ProviderCallUsage",
    "ProviderUsage",
    "TokenPricing",
    "combine_provider_usage",
]
