# migrations

Alembic migrations for the canonical PostgreSQL store.

Current head: `0053_external_cleanup_intents`.

Rules ([`AGENTS.md`](../AGENTS.md) §7,
[`MIGRATION_POLICY.md`](../docs/MIGRATION_POLICY.md), and
[`DECISIONS.md`](../docs/DECISIONS.md) ADR-007):

- PostgreSQL-portable DDL; no provider-specific features in correctness paths.
- Migrations run only through explicit operator commands (`make migrate`) or
  CI checks — never automatically at application startup, so web replicas
  cannot race on schema changes.
- Expand/contract pattern for zero-downtime changes. Every new head also
  updates `packages/db/tests/test_migrations.py` and the RLS registry/test when
  it introduces tenant-owned data.
- The migrator owns DDL; API/worker connect as non-owner `soa_app` with forced
  RLS and DML/sequence-only grants.

Commands:

```bash
make migrate                                  # upgrade to head
uv run alembic revision -m "description"      # new migration
uv run alembic downgrade -1                   # local/schema-cycle testing only
```

Production rollback normally redeploys N-1 code against the expanded schema;
it does not run an emergency down-migration. Security migration `0045` has an
intentional no-op downgrade for privilege tightening, so a downgrade/upgrade
cycle tests compatibility rather than restoring unsafe role membership.
