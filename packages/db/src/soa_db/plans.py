"""Usage and plan model (GTM-001).

A vendor-neutral representation of a commercial plan: what a tenant gets
(included units per metered dimension), how overage is handled (blocked,
charged, or simply allowed), and the price of charged overage. It ties
NOTHING to a billing vendor — GTM-002 handles the export/reconciliation
to whatever billing system the deployment uses.

The point of this module is a SINGLE SOURCE for the plan's limits so that
**product enforcement and billing reporting cannot drift apart**:

- :func:`plan_quotas` derives the hard-limit quotas from the plan. These
  are the exact ``(key, limit_value, limit_unit)`` rows provisioned into
  the ANA-009 quota policy — enforcement never gets a separately
  hand-entered number.
- :func:`reconcile` compares the ANA-003 usage ledger totals against the
  SAME plan's entitlements to produce a billing statement — reporting
  reads the same source enforcement does.

Dimensions are keyed to the ledger's ``billed_unit`` values (``documents``,
``pages``, ``exports``, ``cost_cents``, …) so usage and entitlements line
up by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

__all__ = [
    "Entitlement",
    "LineCharge",
    "OveragePolicy",
    "Plan",
    "PlanError",
    "QuotaSpec",
    "Statement",
    "plan_quotas",
    "reconcile",
]

_CENT = Decimal("1")


class OveragePolicy(StrEnum):
    #: Hard cap — usage beyond the included amount is REJECTED. Maps to an
    #: ANA-009 quota that enforcement checks inline.
    BLOCK = "block"
    #: Metered — usage beyond included is billed at the overage price.
    CHARGE = "charge"
    #: Soft — tracked for reporting, never blocked or billed.
    ALLOW = "allow"


class PlanError(ValueError):
    """A malformed plan or entitlement."""


@dataclass(frozen=True)
class QuotaSpec:
    """A hard limit derived from a plan, in the shape ANA-009 stores."""

    key: str
    limit_value: int
    limit_unit: str


@dataclass(frozen=True)
class Entitlement:
    dimension: str  # a ledger billed_unit, e.g. "documents"
    included: Decimal  # units included in the plan period
    overage: OveragePolicy
    #: Required for CHARGE, forbidden otherwise.
    overage_unit_price_cents: Decimal | None = None
    #: The ANA-009 quota key a BLOCK entitlement provisions; required for
    #: BLOCK, forbidden otherwise.
    quota_key: str | None = None

    def __post_init__(self) -> None:
        if not self.dimension.strip():
            raise PlanError("entitlement dimension must be non-empty")
        if self.included < 0:
            raise PlanError(f"{self.dimension}: included units cannot be negative")
        if self.overage is OveragePolicy.CHARGE:
            price = self.overage_unit_price_cents
            if price is None or price < 0:
                raise PlanError(
                    f"{self.dimension}: a CHARGE entitlement needs a non-negative "
                    "overage_unit_price_cents"
                )
            if self.quota_key is not None:
                raise PlanError(f"{self.dimension}: only BLOCK entitlements take a quota_key")
        elif self.overage is OveragePolicy.BLOCK:
            if self.overage_unit_price_cents is not None:
                raise PlanError(f"{self.dimension}: a BLOCK entitlement is not billed for overage")
            if not (self.quota_key or "").strip():
                raise PlanError(f"{self.dimension}: a BLOCK entitlement needs a quota_key")
            if self.included != self.included.to_integral_value():
                raise PlanError(
                    f"{self.dimension}: a BLOCK entitlement's included units must be whole "
                    "(a quota limit is an integer)"
                )
        else:  # ALLOW
            if self.overage_unit_price_cents is not None or self.quota_key is not None:
                raise PlanError(
                    f"{self.dimension}: an ALLOW entitlement takes no price or quota_key"
                )


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    period: str  # e.g. "monthly"
    entitlements: tuple[Entitlement, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.name.strip() or not self.period.strip():
            raise PlanError("plan key, name, and period are required")
        if not self.entitlements:
            raise PlanError(f"plan {self.key!r} has no entitlements")
        dimensions = [e.dimension for e in self.entitlements]
        if len(dimensions) != len(set(dimensions)):
            raise PlanError(f"plan {self.key!r} has duplicate entitlement dimensions")
        quota_keys = [e.quota_key for e in self.entitlements if e.quota_key]
        if len(quota_keys) != len(set(quota_keys)):
            raise PlanError(f"plan {self.key!r} reuses a quota_key across entitlements")

    def entitlement(self, dimension: str) -> Entitlement | None:
        return next((e for e in self.entitlements if e.dimension == dimension), None)


def plan_quotas(plan: Plan) -> tuple[QuotaSpec, ...]:
    """The hard-limit quotas this plan implies — the single source
    enforcement (ANA-009) provisions, never a hand-entered duplicate."""
    return tuple(
        QuotaSpec(key=str(e.quota_key), limit_value=int(e.included), limit_unit=e.dimension)
        for e in plan.entitlements
        if e.overage is OveragePolicy.BLOCK
    )


@dataclass(frozen=True)
class LineCharge:
    dimension: str
    included: Decimal
    used: Decimal
    overage_units: Decimal
    policy: OveragePolicy
    charge_cents: int
    #: True when a BLOCK dimension's usage exceeded its cap — enforcement
    #: should have prevented it; reporting surfaces it if it slipped through.
    over_limit: bool


@dataclass(frozen=True)
class Statement:
    plan_key: str
    period: str
    lines: tuple[LineCharge, ...]
    total_overage_cents: int

    @property
    def over_limit_dimensions(self) -> tuple[str, ...]:
        return tuple(line.dimension for line in self.lines if line.over_limit)


def reconcile(plan: Plan, usage: Mapping[str, Decimal]) -> Statement:
    """Compare actual usage (per dimension, from the ANA-003 ledger
    totals) against the plan's entitlements. A dimension with no usage is
    zero; usage for a dimension the plan does not meter is ignored here
    (it belongs to a different plan or is untracked)."""
    lines: list[LineCharge] = []
    total = 0
    for entitlement in plan.entitlements:
        used = usage.get(entitlement.dimension, Decimal(0))
        if used < 0:
            raise PlanError(f"{entitlement.dimension}: usage cannot be negative")
        overage = max(Decimal(0), used - entitlement.included)
        charge = 0
        over_limit = False
        if entitlement.overage is OveragePolicy.CHARGE and overage > 0:
            price = entitlement.overage_unit_price_cents or Decimal(0)
            charge = int((overage * price).quantize(_CENT, rounding=ROUND_HALF_UP))
        elif entitlement.overage is OveragePolicy.BLOCK:
            over_limit = used > entitlement.included
        lines.append(
            LineCharge(
                dimension=entitlement.dimension,
                included=entitlement.included,
                used=used,
                overage_units=overage,
                policy=entitlement.overage,
                charge_cents=charge,
                over_limit=over_limit,
            )
        )
        total += charge
    return Statement(
        plan_key=plan.key, period=plan.period, lines=tuple(lines), total_overage_cents=total
    )
