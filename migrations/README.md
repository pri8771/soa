# migrations

Alembic migrations for the canonical PostgreSQL store.

Rules (docs/AGENTS.md §7, docs/DECISIONS.md ADR-007):

- PostgreSQL-portable DDL; no provider-specific features in correctness paths.
- Migrations run only through explicit operator commands (`make migrate`) or
  CI checks — never automatically at application startup, so web replicas
  cannot race on schema changes.
- Expand/contract pattern for zero-downtime changes once in production.

Commands:

```bash
make migrate                                  # upgrade to head
uv run alembic revision -m "description"      # new migration
uv run alembic downgrade -1                   # step back one revision
```
