"""Monthly billing statement tests (GTM-002).

The documented rules — documents counted once, pages governed by the
reprocess policy, provider cost never affected by that policy, exports
counted once — plus the honesty separation between what the customer is
billed and what the platform spent.
"""

from decimal import Decimal

import pytest

from soa_db.billing_statement import (
    DocumentUsage,
    ReprocessPolicy,
    meter_usage,
    monthly_statement,
)
from soa_db.plans import Entitlement, OveragePolicy, Plan


def _plan() -> Plan:
    return Plan(
        key="pilot",
        name="Pilot",
        period="monthly",
        entitlements=(
            Entitlement(
                dimension="documents",
                included=Decimal(2),
                overage=OveragePolicy.CHARGE,
                overage_unit_price_cents=Decimal(50),
            ),
            Entitlement(
                dimension="pages",
                included=Decimal(10),
                overage=OveragePolicy.CHARGE,
                overage_unit_price_cents=Decimal(5),
            ),
            Entitlement(
                dimension="exports",
                included=Decimal(100),
                overage=OveragePolicy.BLOCK,
                quota_key="quota.monthly_exports",
            ),
        ),
    )


class TestMeteringRules:
    def test_documents_count_once_regardless_of_reprocessing(self) -> None:
        docs = [
            DocumentUsage("d1", run_count=3, pages=4, exports=1, provider_cost_cents=90),
            DocumentUsage("d2", run_count=1, pages=2, exports=1, provider_cost_cents=20),
        ]
        usage = meter_usage(docs)
        assert usage.documents == 2  # not 4 (the reprocesses of d1 do not add documents)

    def test_first_run_only_bills_pages_once_across_reprocesses(self) -> None:
        docs = [DocumentUsage("d1", run_count=3, pages=4, exports=0, provider_cost_cents=0)]
        usage = meter_usage(docs, reprocess=ReprocessPolicy.BILL_FIRST_RUN_ONLY)
        assert usage.pages == 4  # billed once
        assert usage.reprocess_runs == 2

    def test_each_run_bills_pages_every_run(self) -> None:
        docs = [DocumentUsage("d1", run_count=3, pages=4, exports=0, provider_cost_cents=0)]
        usage = meter_usage(docs, reprocess=ReprocessPolicy.BILL_EACH_RUN)
        assert usage.pages == 12  # 4 pages x 3 runs

    def test_provider_cost_is_summed_as_is_regardless_of_policy(self) -> None:
        docs = [DocumentUsage("d1", run_count=3, pages=4, exports=0, provider_cost_cents=90)]
        # The platform really spent the money each run — the reprocess
        # policy waives customer pages, never the actual provider spend.
        first = meter_usage(docs, reprocess=ReprocessPolicy.BILL_FIRST_RUN_ONLY)
        each = meter_usage(docs, reprocess=ReprocessPolicy.BILL_EACH_RUN)
        assert first.provider_cost_cents == 90
        assert each.provider_cost_cents == 90

    def test_exports_are_summed(self) -> None:
        docs = [
            DocumentUsage("d1", run_count=1, pages=1, exports=2, provider_cost_cents=0),
            DocumentUsage("d2", run_count=1, pages=1, exports=1, provider_cost_cents=0),
        ]
        assert meter_usage(docs).exports == 3


class TestStatement:
    def test_customer_charge_is_overage_not_provider_cost(self) -> None:
        docs = [
            DocumentUsage("d1", run_count=1, pages=8, exports=1, provider_cost_cents=300),
            DocumentUsage("d2", run_count=1, pages=8, exports=1, provider_cost_cents=300),
            DocumentUsage("d3", run_count=1, pages=8, exports=1, provider_cost_cents=300),
        ]
        statement = monthly_statement(_plan(), docs, period_label="2026-07")
        # 3 documents (1 over the 2 included -> 50c); 24 pages (14 over
        # 10 -> 14 x 5 = 70c). Total customer charge = 120c.
        assert statement.customer_charge_cents == 120
        # Provider cost is reported separately, never folded in.
        assert statement.provider_cost_cents == 900
        assert statement.customer_charge_cents != statement.provider_cost_cents

    def test_reprocess_policy_changes_customer_pages_but_not_provider_cost(self) -> None:
        docs = [DocumentUsage("d1", run_count=4, pages=8, exports=0, provider_cost_cents=400)]
        first = monthly_statement(
            _plan(), docs, period_label="2026-07", reprocess=ReprocessPolicy.BILL_FIRST_RUN_ONLY
        )
        each = monthly_statement(
            _plan(), docs, period_label="2026-07", reprocess=ReprocessPolicy.BILL_EACH_RUN
        )
        # First-run-only: 8 pages, 0 over -> no page charge (1 doc within
        # the 2 included). Each-run: 32 pages, 22 over -> 22 x 5 = 110c.
        assert first.customer_charge_cents == 0
        assert each.customer_charge_cents == 110
        assert first.provider_cost_cents == each.provider_cost_cents == 400

    def test_statement_is_recomputable_and_deterministic(self) -> None:
        docs = [DocumentUsage("d1", run_count=2, pages=5, exports=1, provider_cost_cents=50)]
        a = monthly_statement(_plan(), docs, period_label="2026-07")
        b = monthly_statement(_plan(), docs, period_label="2026-07")
        assert a.usage == b.usage
        assert a.plan_statement == b.plan_statement

    def test_over_limit_block_dimension_is_surfaced(self) -> None:
        # 101 exports against a BLOCK cap of 100 — enforcement should have
        # stopped it; the statement surfaces it if it slipped through.
        docs = [
            DocumentUsage(f"d{i}", run_count=1, pages=1, exports=1, provider_cost_cents=0)
            for i in range(101)
        ]
        statement = monthly_statement(_plan(), docs, period_label="2026-07")
        assert statement.over_limit_dimensions == ("exports",)
        # A BLOCK dimension is never billed for overage.
        assert statement.customer_charge_cents == 0 or "exports" not in {
            line.dimension for line in statement.plan_statement.lines if line.charge_cents
        }

    def test_empty_period_produces_a_zero_statement(self) -> None:
        statement = monthly_statement(_plan(), [], period_label="2026-07")
        assert statement.usage.documents == 0
        assert statement.customer_charge_cents == 0
        assert statement.provider_cost_cents == 0


class TestValidation:
    def test_run_count_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError):
            DocumentUsage("d1", run_count=0, pages=1, exports=0, provider_cost_cents=0)

    def test_negative_pages_refused(self) -> None:
        with pytest.raises(ValueError):
            DocumentUsage("d1", run_count=1, pages=-1, exports=0, provider_cost_cents=0)

    def test_empty_period_label_refused(self) -> None:
        with pytest.raises(ValueError):
            monthly_statement(_plan(), [], period_label="  ")

    def test_provider_credit_is_allowed(self) -> None:
        # A net credit after reconciling a provider overcharge is valid.
        usage = meter_usage(
            [DocumentUsage("d1", run_count=1, pages=1, exports=0, provider_cost_cents=-25)]
        )
        assert usage.provider_cost_cents == -25
