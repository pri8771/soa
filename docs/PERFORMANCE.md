# Performance budgets and testing (REL-009)

How the platform sets performance targets and measures deterministic hot
paths. Only the three CI microbenchmarks are enforced today; the deployed
system/load gate described below is still release work.

The source of truth is `packages/config/src/soa_config/performance.py`
(budgets, benchmark harness, regression gate) with the suite in
`tests/performance/`. The stored per-release baseline lives in
`perf/baseline.json` and accepted regressions in `perf/waivers.json`.

## Budgets

One budget per required workload area, each with a threshold and the
reasoning behind it:

| Key | Area | Threshold | Measured |
| --- | --- | --- | --- |
| `catalog.match_index` | catalog matching | < 2.0s to index 5000 records + 1000 matches | CI |
| `normalize.throughput` | line grid | < 1.5s to normalize 20000 cell values | CI |
| `rules.evaluate` | line grid | < 3.0s to evaluate the baseline rules over 200× a 10-line order | CI |
| `api.read_p95` | API | p95 < 200ms at 50 rps / instance | staging |
| `queue.throughput` | queue | ≥ 50 jobs/s at 8 workers | staging |
| `viewer.first_render` | viewer | < 1.5s to first interactive page (p95) | staging |
| `worker.concurrency_scaling` | worker concurrency | ≥ 0.8 scaling efficiency to 8 replicas | staging |
| `export.burst` | export burst | drain 500 exports < 60s, no loss | staging |

## Two layers of enforcement

**CI (non-flaky, absolute ceilings).** The deterministic, in-process
workloads — catalog matching, cell normalization, rule evaluation — run
in `tests/performance/` under **generous absolute ceilings** (roughly
10× the observed cost). They catch a complexity regression (an
accidental O(n²), a per-row re-parse) on any runner without failing on
timing noise. These run in the normal Python CI job.

**Staging (required, not implemented as an automated runner).** The
system-level workloads (API, queue, viewer, worker concurrency, export burst)
need a deployed system, representative objects/documents, traffic generation,
and metric collection. They are therefore marked `ci_measured=False`. The
repository defines their budgets and a generic `compare_to_baseline` policy,
but it does not contain a staging load driver or system-level baseline for
those five workloads. Before release, add the executable scenarios, capture a
production-equivalent baseline, wire the comparison into the release gate, and
exercise time-limited waivers (owner + reason + expiry). Until then these
budgets are targets, not passed checks.

## Updating the baseline

When a legitimate, reviewed change moves a CI-measured cost, refresh the
relevant number in `perf/baseline.json` in the same PR and say why. Add
system-level baseline keys only when the corresponding executable staging
scenario produces them. A baseline is a record of what "normal" is; it should
change deliberately, never drift.

## Running locally

```
make perf
```

runs the performance suite (harness checks + the CI-measured
benchmarks).
