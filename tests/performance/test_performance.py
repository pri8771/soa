"""Performance test suite (REL-009).

Two kinds of check:

- **Catalog & harness validation** — the budget catalog covers every
  required workload area, and the benchmark/regression machinery behaves.
- **CI-measured micro-benchmarks** — the deterministic, in-process
  workloads (catalog matching, cell normalization, rule evaluation) run
  under a GENEROUS absolute ceiling that catches a complexity regression
  without flaking on runner noise. System-level workloads (API, queue,
  viewer, worker concurrency, export burst) are budgeted but measured in
  staging load tests, not here — asserted by ``ci_measured=False``.
"""

import json
import uuid
from datetime import date
from pathlib import Path

from soa_config.performance import (
    BUDGETS,
    Benchmark,
    BenchmarkResult,
    Waiver,
    WorkloadArea,
    budget_for,
    compare_to_baseline,
)

TODAY = date(2026, 7, 13)
_PERF_DIR = Path(__file__).resolve().parents[2] / "perf"


class TestBudgetCatalog:
    def test_every_workload_area_has_a_budget(self) -> None:
        covered = {b.area for b in BUDGETS}
        assert covered == set(WorkloadArea), "every REL-009 workload area needs a budget"

    def test_budget_keys_unique_and_specified(self) -> None:
        keys = [b.key for b in BUDGETS]
        assert len(keys) == len(set(keys))
        for budget in BUDGETS:
            assert budget.threshold.strip()
            assert len(budget.rationale) > 40, f"{budget.key} rationale too thin"

    def test_ci_measured_budgets_are_exercised(self) -> None:
        # Each ci_measured budget must have a benchmark below; guard that
        # the two lists stay in sync.
        measured = {b.key for b in BUDGETS if b.ci_measured}
        assert measured == {"catalog.match_index", "normalize.throughput", "rules.evaluate"}

    def test_stored_baseline_covers_every_measured_budget(self) -> None:
        baseline = json.loads((_PERF_DIR / "baseline.json").read_text())["baseline"]
        measured = {b.key for b in BUDGETS if b.ci_measured}
        assert set(baseline) == measured, "perf/baseline.json must record every CI-measured budget"
        assert all(v > 0 for v in baseline.values())

    def test_waivers_file_is_valid_and_ships_empty(self) -> None:
        waivers = json.loads((_PERF_DIR / "waivers.json").read_text())["waivers"]
        assert waivers == [], "the platform ships with no accepted performance regressions"


class TestHarness:
    def test_benchmark_reports_statistics(self) -> None:
        ticks = iter([0.0, 1.0, 1.0, 3.0, 3.0, 6.0])  # three runs: 1s, 2s, 3s
        bench = Benchmark(clock=lambda: next(ticks))
        result = bench.run("x", lambda: None, iterations=3)
        assert result.iterations == 3
        assert result.median_seconds == 2.0
        assert result.total_seconds == 6.0
        assert result.p95_seconds == 3.0

    def test_regression_gate_flags_slowdowns_beyond_tolerance(self) -> None:
        results = [
            BenchmarkResult("a", 1, 0.10, 0.10, 0.10),  # baseline 0.10, within tol
            BenchmarkResult("b", 1, 0.50, 0.50, 0.50),  # baseline 0.20, 2.5x -> regression
            BenchmarkResult("c", 1, 0.10, 0.10, 0.10),  # no baseline -> skipped
        ]
        baseline = {"a": 0.10, "b": 0.20}
        regressions = compare_to_baseline(results, baseline, tolerance=0.25, today=TODAY)
        assert [r.key for r in regressions] == ["b"]
        assert regressions[0].blocking is True

    def test_active_waiver_makes_a_regression_non_blocking(self) -> None:
        results = [BenchmarkResult("b", 1, 0.50, 0.50, 0.50)]
        baseline = {"b": 0.20}
        waiver = Waiver("b", "known cost of the new fuzzy tier", "perf@x", date(2026, 12, 31))
        (regression,) = compare_to_baseline(results, baseline, waivers=[waiver], today=TODAY)
        assert regression.blocking is False
        assert regression.waived_by is waiver

    def test_expired_waiver_does_not_excuse(self) -> None:
        results = [BenchmarkResult("b", 1, 0.50, 0.50, 0.50)]
        waiver = Waiver("b", "stale", "perf@x", date(2026, 1, 1))  # expired
        (regression,) = compare_to_baseline(results, {"b": 0.20}, waivers=[waiver], today=TODAY)
        assert regression.blocking is True


class TestCatalogMatchingBenchmark:
    def test_index_build_and_match_within_budget(self) -> None:
        from soa_db.catalog_matching import CatalogMatchIndex, RecordFacts

        records = [
            RecordFacts(
                record_id=uuid.uuid4(),
                source_id=f"SKU-{i:05d}",
                display_name=f"Widget model {i}",
                aliases=(f"W{i}", f"widget-{i}"),
            )
            for i in range(5000)
        ]
        queries = [f"SKU-{i:05d}" for i in range(0, 5000, 5)] + [
            f"Widget model {i}" for i in range(0, 5000, 5)
        ]

        def work() -> None:
            index = CatalogMatchIndex(records)
            for q in queries:
                index.match(q)

        result = Benchmark().run(budget_for("catalog.match_index").key, work, iterations=3)
        # Generous ceiling: catches an accidental O(n^2), ignores noise.
        assert result.median_seconds < 2.0, (
            f"catalog matching too slow: {result.median_seconds:.3f}s"
        )


class TestNormalizationBenchmark:
    def test_bulk_normalization_within_budget(self) -> None:
        from soa_normalize.text import normalize_identifier, normalize_whitespace

        values = [f"  SKU_{i}-00{i} \t model  " for i in range(20000)]

        def work() -> None:
            for v in values:
                normalize_whitespace(v)
                normalize_identifier(v)

        result = Benchmark().run(budget_for("normalize.throughput").key, work, iterations=3)
        assert result.median_seconds < 1.5, f"normalization too slow: {result.median_seconds:.3f}s"


class TestRuleEvaluationBenchmark:
    def test_baseline_rule_set_evaluation_within_budget(self) -> None:
        from soa_rules import EvaluationInput, evaluate_rule_set
        from soa_rules.baseline import baseline_sales_order_rules

        rules = baseline_sales_order_rules()
        data = EvaluationInput(
            header={
                "order_number": "PO-1000",
                "order_date": "2026-07-01",
                "currency": "USD",
                "total_amount": "1200.00",
            },
            tables={
                "lines": [
                    {
                        "lines.sku": f"SKU-{i}",
                        "lines.quantity": "3",
                        "lines.unit_price": "100.00",
                        "lines.line_total": "300.00",
                    }
                    for i in range(10)
                ]
            },
        )

        def work() -> None:
            for _ in range(200):
                evaluate_rule_set(rules, data)

        result = Benchmark().run(budget_for("rules.evaluate").key, work, iterations=3)
        assert result.median_seconds < 3.0, (
            f"rule evaluation too slow: {result.median_seconds:.3f}s"
        )
