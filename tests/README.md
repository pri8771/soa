# Cross-application tests

The root suite contains behavior that spans application/package ownership:
release contracts, the local supervisor, security and PostgreSQL/RLS checks,
performance microbenchmarks, resilience invariants, and evaluation flows.
App/package-local unit and component tests remain beside their owners under
`apps/*/tests` and `packages/*/tests`; browser tests live under `apps/web/e2e`.

```bash
uv run pytest -q
volta run --node 22 pnpm run test
make test-e2e
make test-visual
```

PostgreSQL-marked tests require `SOA_TEST_POSTGRES_URL`. Browser visual tests
use the pinned Linux renderer in CI. Live provider/ERP, deployed load/fault,
Terraform apply, backup/restore, and manual accessibility/usability checks are
separate release gates; a skipped external test is unverified, not passed.
