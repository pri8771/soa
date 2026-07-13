"""Performance budgets, benchmark harness, and regression gate (REL-009).

Three parts, kept provider-agnostic and testable:

1. **Budgets** — a declarative catalog of the workloads the platform
   commits to and their thresholds, one per required area (API, queue,
   viewer, line grid, catalog matching, worker concurrency, export
   burst). Each budget states WHY the threshold is what it is and WHERE
   it is measured.

2. **Harness** — :class:`Benchmark` times a callable over N iterations
   and reports median / p95 seconds, so the deterministic algorithmic
   workloads (catalog matching, normalization, rule evaluation) get a
   real, repeatable measurement in CI under a GENEROUS absolute ceiling
   that catches complexity regressions (an accidental O(n^2)) without
   flaking on runner noise.

3. **Regression gate** — :func:`compare_to_baseline` diffs a run against
   a stored per-release baseline and flags anything slower than its
   tolerance, unless a TIME-LIMITED :class:`Waiver` (owner + reason +
   expiry, mirroring the SEC-011 exception model) covers it. This is how
   "regressions beyond threshold block release or require a waiver" is
   enforced.

Honesty: workloads that need a deployed system (API p95 under load,
queue throughput, viewer interaction latency, worker concurrency, export
burst) are marked ``ci_measured=False`` — they are exercised by the
staging load tests that REL-002 infrastructure stands up, not faked in a
unit run. The algorithmic budgets are measured here.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

__all__ = [
    "BUDGETS",
    "Benchmark",
    "BenchmarkResult",
    "PerformanceBudget",
    "Regression",
    "Waiver",
    "WorkloadArea",
    "budget_for",
    "compare_to_baseline",
]


class WorkloadArea(StrEnum):
    API = "api"
    QUEUE = "queue"
    VIEWER = "viewer"
    LINE_GRID = "line_grid"
    CATALOG_MATCHING = "catalog_matching"
    WORKER_CONCURRENCY = "worker_concurrency"
    EXPORT_BURST = "export_burst"


@dataclass(frozen=True)
class PerformanceBudget:
    key: str
    area: WorkloadArea
    description: str
    threshold: str  # human-readable target, e.g. "p95 < 200ms at 50 rps"
    rationale: str
    #: True when the CI performance suite measures this directly; False
    #: when it needs a deployed system (staging load test, REL-002).
    ci_measured: bool


BUDGETS: tuple[PerformanceBudget, ...] = (
    PerformanceBudget(
        key="catalog.match_index",
        area=WorkloadArea.CATALOG_MATCHING,
        description="Build a match index over a large catalog and resolve many queries",
        threshold="< 2.0s to index 5000 records and run 1000 matches (CI ceiling)",
        rationale=(
            "Matching is deterministic and in-process (CAT-006/007); a linear index "
            "build plus dict-lookup matches must stay well under a second of real work, "
            "so a generous 2s ceiling catches an accidental quadratic without flaking."
        ),
        ci_measured=True,
    ),
    PerformanceBudget(
        key="normalize.throughput",
        area=WorkloadArea.LINE_GRID,
        description="Normalize a large batch of line-cell values (the review grid's hot path)",
        threshold="< 1.0s to normalize 20000 values (CI ceiling)",
        rationale=(
            "The line-item grid (REV-008) normalizes every cell; normalization is pure "
            "string/number work (PRC-008) and must scale linearly with cell count."
        ),
        ci_measured=True,
    ),
    PerformanceBudget(
        key="rules.evaluate",
        area=WorkloadArea.LINE_GRID,
        description="Evaluate the baseline rule set over many documents",
        threshold="< 1.5s to evaluate 2000 documents (CI ceiling)",
        rationale=(
            "Rule evaluation (PRC-009/010) runs on every processed document and every "
            "review revalidation; it is deterministic AST walking and must not regress "
            "into re-parsing or repeated work per rule."
        ),
        ci_measured=True,
    ),
    PerformanceBudget(
        key="api.read_p95",
        area=WorkloadArea.API,
        description="Tenant-scoped read endpoints under sustained load",
        threshold="p95 < 200ms at 50 rps per instance",
        rationale=(
            "Interactive console reads must feel instant; 200ms p95 keeps the UI "
            "responsive. Measured against a deployed instance with a real database, "
            "not in a unit run."
        ),
        ci_measured=False,
    ),
    PerformanceBudget(
        key="queue.throughput",
        area=WorkloadArea.QUEUE,
        description="Job claim + completion throughput with N workers",
        threshold="≥ 50 jobs/s aggregate at 8 workers with no lock contention collapse",
        rationale=(
            "The SKIP LOCKED claim loop (JOB-003) must scale with workers; throughput "
            "that flatlines as workers are added signals contention. Needs a real "
            "Postgres and multiple worker processes (staging)."
        ),
        ci_measured=False,
    ),
    PerformanceBudget(
        key="viewer.first_render",
        area=WorkloadArea.VIEWER,
        description="Document viewer first-page render and overlay paint",
        threshold="< 1.5s to first interactive page at p95",
        rationale=(
            "Reviewers open the viewer constantly (REV-004/005); a slow first render "
            "dominates their perceived latency. Measured in-browser against built "
            "assets (staging / Playwright timing), not a unit run."
        ),
        ci_measured=False,
    ),
    PerformanceBudget(
        key="worker.concurrency_scaling",
        area=WorkloadArea.WORKER_CONCURRENCY,
        description="End-to-end document throughput as worker replicas scale",
        threshold="near-linear scaling to 8 replicas (≥ 0.8 efficiency)",
        rationale=(
            "Adding workers must add throughput; sub-linear scaling reveals a shared "
            "bottleneck (DB, storage, a provider). Needs the full pipeline deployed."
        ),
        ci_measured=False,
    ),
    PerformanceBudget(
        key="export.burst",
        area=WorkloadArea.EXPORT_BURST,
        description="Export orchestration under a burst of approved orders",
        threshold="drain 500 queued exports < 60s with no delivery loss",
        rationale=(
            "Approvals arrive in bursts (end of day); the export pipeline (EXP-008) "
            "must drain them promptly and idempotently. Measured against real "
            "receivers/adapters in staging."
        ),
        ci_measured=False,
    ),
)


def budget_for(key: str) -> PerformanceBudget:
    for budget in BUDGETS:
        if budget.key == key:
            return budget
    raise KeyError(f"no performance budget named {key!r}")


@dataclass(frozen=True)
class BenchmarkResult:
    key: str
    iterations: int
    median_seconds: float
    p95_seconds: float
    total_seconds: float


@dataclass
class Benchmark:
    """Time a callable over N iterations. ``clock`` is injectable so
    tests can assert the statistics deterministically."""

    clock: Callable[[], float] = time.perf_counter

    def run(self, key: str, work: Callable[[], object], *, iterations: int) -> BenchmarkResult:
        if iterations < 1:
            raise ValueError("iterations must be >= 1")
        samples: list[float] = []
        for _ in range(iterations):
            start = self.clock()
            work()
            samples.append(self.clock() - start)
        samples.sort()
        return BenchmarkResult(
            key=key,
            iterations=iterations,
            median_seconds=statistics.median(samples),
            p95_seconds=samples[min(len(samples) - 1, round(0.95 * (len(samples) - 1)))],
            total_seconds=sum(samples),
        )


@dataclass(frozen=True)
class Waiver:
    """A time-limited, attributed acceptance of a known regression —
    same shape as the SEC-011 vulnerability exception."""

    key: str
    reason: str
    owner: str
    expires: date

    def is_active(self, today: date) -> bool:
        return today <= self.expires


@dataclass(frozen=True)
class Regression:
    key: str
    baseline_seconds: float
    current_seconds: float
    ratio: float
    waived_by: Waiver | None = None

    @property
    def blocking(self) -> bool:
        return self.waived_by is None


def compare_to_baseline(
    results: Iterable[BenchmarkResult],
    baseline: dict[str, float],
    *,
    tolerance: float = 0.25,
    waivers: Iterable[Waiver] = (),
    today: date,
) -> list[Regression]:
    """Flag results slower than ``baseline[key] * (1 + tolerance)``.

    A result with no baseline entry is skipped (new benchmark — record it
    first). An active waiver marks a regression non-blocking; an expired
    one does not. Uses the p95 as the comparison statistic.
    """
    active = {w.key: w for w in waivers if w.is_active(today)}
    regressions: list[Regression] = []
    for result in results:
        reference = baseline.get(result.key)
        if reference is None or reference <= 0:
            continue
        ceiling = reference * (1.0 + tolerance)
        if result.p95_seconds > ceiling:
            regressions.append(
                Regression(
                    key=result.key,
                    baseline_seconds=reference,
                    current_seconds=result.p95_seconds,
                    ratio=result.p95_seconds / reference,
                    waived_by=active.get(result.key),
                )
            )
    return regressions
