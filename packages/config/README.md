# `soa-config`

Shared runtime policy and infrastructure-neutral configuration for the Python
services. This is an active uv-workspace package, not a repository of lint or
build-tool presets.

## Responsibilities

- `settings.py` provides frozen Pydantic service settings, environment
  profiles, masked serialization, and fail-closed production validation for
  database credentials, secret stores, debugging, and telemetry.
- `secrets.py` defines opaque `secretref://` parsing, the `SecretStore`
  protocol, and development memory/environment/file implementations. Managed
  AWS and GCP implementations live in `soa-storage` and implement this
  contract.
- `logging.py` provides correlation context, structured JSON logging, and
  recursive sensitive-key redaction.
- `telemetry.py` configures no-op, console, or OTLP/HTTP tracing and keeps
  exporter failures off the business path.
- `alerts.py` is the validated alert catalog: severity, symbolic owner,
  signal, threshold, test signal, and runbook slug. Deployment-specific
  monitoring policies and real pager mappings are intentionally outside this
  package.
- `performance.py` defines performance budgets, benchmark results, baselines,
  expiring waivers, and regression comparison. It does not run staging load
  tests by itself.
- `supply_chain.py` normalizes Python/JavaScript audit reports and applies the
  repository's finding/exception policy.
- `provider_catalog.py` defines safe provider-catalog metadata, warnings, and
  routing previews. Runtime tenant policy, credentials, health, and provider
  invocation remain API/worker/database responsibilities.

## Verification

```bash
uv run pytest packages/config/tests
uv run mypy packages/config/src
uv run ruff check packages/config
```

Do not add provider SDKs, database access, web framework state, or raw secret
values here. Keep this package deterministic and reusable by both API and
worker composition roots.
