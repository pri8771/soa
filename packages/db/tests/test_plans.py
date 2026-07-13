"""Usage and plan model tests (GTM-001).

The plan is the single source of truth for limits: the quotas enforcement
provisions and the billing statement reporting produces both derive from
the SAME plan, so they cannot drift. Plus overage-policy semantics and
plan/entitlement validation.
"""

from decimal import Decimal

import pytest

from soa_db.plans import (
    Entitlement,
    OveragePolicy,
    Plan,
    PlanError,
    plan_quotas,
    reconcile,
)


def _pilot_plan() -> Plan:
    return Plan(
        key="pilot",
        name="Pilot",
        period="monthly",
        entitlements=(
            Entitlement(
                dimension="documents",
                included=Decimal(1000),
                overage=OveragePolicy.CHARGE,
                overage_unit_price_cents=Decimal("2.5"),
            ),
            Entitlement(
                dimension="exports",
                included=Decimal(500),
                overage=OveragePolicy.BLOCK,
                quota_key="quota.monthly_exports",
            ),
            Entitlement(
                dimension="pages",
                included=Decimal(20000),
                overage=OveragePolicy.ALLOW,
            ),
        ),
    )


class TestSingleSource:
    def test_quotas_derive_only_from_block_entitlements(self) -> None:
        (quota,) = plan_quotas(_pilot_plan())
        # Only the BLOCK dimension becomes a hard quota — and its value is
        # the plan's included units, not a separately entered number.
        assert quota.key == "quota.monthly_exports"
        assert quota.limit_value == 500
        assert quota.limit_unit == "exports"

    def test_enforcement_and_reporting_agree_on_the_limit(self) -> None:
        plan = _pilot_plan()
        (quota,) = plan_quotas(plan)
        statement = reconcile(plan, {"exports": Decimal(500)})
        exports_line = next(line for line in statement.lines if line.dimension == "exports")
        # The quota enforcement uses and the included amount reporting uses
        # are the same number from the same plan.
        assert Decimal(quota.limit_value) == exports_line.included


class TestOverageSemantics:
    def test_charge_bills_overage_at_the_unit_price(self) -> None:
        plan = _pilot_plan()
        statement = reconcile(plan, {"documents": Decimal(1400)})
        docs = next(line for line in statement.lines if line.dimension == "documents")
        # 400 documents over, at 2.5 cents each = 1000 cents.
        assert docs.overage_units == Decimal(400)
        assert docs.charge_cents == 1000
        assert statement.total_overage_cents == 1000
        assert not statement.over_limit_dimensions

    def test_block_flags_over_limit_but_never_bills(self) -> None:
        statement = reconcile(_pilot_plan(), {"exports": Decimal(501)})
        exports = next(line for line in statement.lines if line.dimension == "exports")
        assert exports.over_limit is True
        assert exports.charge_cents == 0
        assert statement.over_limit_dimensions == ("exports",)

    def test_allow_tracks_without_charge_or_block(self) -> None:
        statement = reconcile(_pilot_plan(), {"pages": Decimal(50000)})
        pages = next(line for line in statement.lines if line.dimension == "pages")
        assert pages.overage_units == Decimal(30000)
        assert pages.charge_cents == 0
        assert pages.over_limit is False

    def test_usage_within_plan_has_no_overage(self) -> None:
        statement = reconcile(_pilot_plan(), {"documents": Decimal(10), "exports": Decimal(1)})
        assert statement.total_overage_cents == 0
        assert not statement.over_limit_dimensions

    def test_unmetered_usage_is_ignored(self) -> None:
        # A dimension the plan does not meter does not appear in the
        # statement and is not charged.
        statement = reconcile(_pilot_plan(), {"unknown_dimension": Decimal(999)})
        assert {line.dimension for line in statement.lines} == {"documents", "exports", "pages"}
        assert statement.total_overage_cents == 0


class TestValidation:
    def test_charge_requires_a_price(self) -> None:
        with pytest.raises(PlanError):
            Entitlement(dimension="d", included=Decimal(1), overage=OveragePolicy.CHARGE)

    def test_block_requires_a_quota_key_and_whole_units(self) -> None:
        with pytest.raises(PlanError):
            Entitlement(dimension="d", included=Decimal(1), overage=OveragePolicy.BLOCK)
        with pytest.raises(PlanError):
            Entitlement(
                dimension="d",
                included=Decimal("1.5"),
                overage=OveragePolicy.BLOCK,
                quota_key="quota.d",
            )

    def test_allow_takes_no_price_or_quota(self) -> None:
        with pytest.raises(PlanError):
            Entitlement(
                dimension="d",
                included=Decimal(1),
                overage=OveragePolicy.ALLOW,
                overage_unit_price_cents=Decimal(1),
            )

    def test_plan_rejects_duplicate_dimensions_and_empty(self) -> None:
        dup = Entitlement(dimension="d", included=Decimal(1), overage=OveragePolicy.ALLOW)
        with pytest.raises(PlanError):
            Plan(key="p", name="P", period="monthly", entitlements=(dup, dup))
        with pytest.raises(PlanError):
            Plan(key="p", name="P", period="monthly", entitlements=())

    def test_negative_usage_is_refused(self) -> None:
        with pytest.raises(PlanError):
            reconcile(_pilot_plan(), {"documents": Decimal(-1)})
