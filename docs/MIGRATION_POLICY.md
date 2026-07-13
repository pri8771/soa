# Rollback & expand/contract migration policy (REL-006)

How the platform changes its schema, configuration, and providers without
taking downtime or trapping itself into a one-way door. The rule behind
all of it: **a running version and the one before it must both work
against the current database**, so a deploy can always be rolled back to
the previous immutable artifact.

## Expand / contract (parallel change)

Never change a column's meaning, name, or nullability in a single step
while code depends on it. Split every breaking schema change into ordered
phases, each shipped and stabilized before the next:

1. **Expand** — add the new shape *additively*: a new nullable column, a
   new table, a new index (created concurrently in production). The old
   code ignores it; the new code can use it. Nothing breaks because
   nothing was removed.
2. **Migrate** — backfill data into the new shape with an online,
   idempotent, batched job (not inside the DDL migration). Dual-write
   from application code if both shapes must stay current during the
   transition.
3. **Contract** — only after the expand is fully deployed and stable, and
   no running version reads the old shape, drop or tighten it (make
   NOT NULL, drop the old column, remove the compat code) in a *later*
   migration.

Because each phase leaves N-1 code working, a rollback is always a code
rollback — never an emergency down-migration in the hot path.

Forbidden in one migration while old code is live: renaming or dropping a
column/table in use, adding a `NOT NULL` column without a default,
narrowing a type, or changing a unique constraint that current writes
depend on.

## Repository conventions (enforced in CI)

- Migrations are sequential and numbered (`migrations/versions/NNNN_*.py`,
  currently through `0037`). Each new migration bumps the head assertion
  in `packages/db/tests/test_migrations.py`.
- Every migration is **reversible**: CI runs `upgrade head → downgrade
  base → upgrade head`, so a `downgrade` that drops what `upgrade` added
  is mandatory. An irreversible step (a destructive contract) is a
  deliberate, reviewed exception documented in the migration.
- New tenant-scoped tables enable and FORCE row-level security with a
  `tenant_isolation` policy in the same migration and add the table name
  to `soa_db.tenant_guard.RLS_PROTECTED_TABLES` — the coverage test fails
  otherwise. A new table without tenant scope must say why in the
  migration.
- Schema DDL and data backfills are separate migrations/jobs: DDL is fast
  and transactional; backfills are online and batched.

## Artifact rollback

Deploys promote the *same* immutable image (REL-001, pinned by digest
under REL-002) through environments; a rollback redeploys the previous
digest. Nothing is rebuilt, so the rolled-back version is byte-identical
to what last passed CI. The expand/contract discipline above is what
makes that safe against the live schema. The mechanical deploy/rollback
workflow that drives this is REL-005 (blocked on the REL-002 platform
choice); this document is the policy it will enforce.

## Configuration and provider rollback

- **Configuration** is versioned and published, never edited live: a bad
  stream/schema/rule change is reverted by rolling back to the prior
  published version (CFG-007), which the comparison/rollback UI (CFG-014)
  drives. Draft changes never affect production until published.
- **Mapping profiles** are versioned the same way (EXP-003); a broken
  mapping is reverted to the last good version and affected orders
  re-exported deterministically (EXP-002).
- **Providers** are selected through the router (AIO-013) behind the
  AIO-001 capability contract, so switching or reverting a provider is a
  resolved-config change, not a code change — and evaluation gates
  (AIO-017) guard a provider change before it reaches production. See
  [`runbooks/bad-release.md`](runbooks/bad-release.md) for the response
  procedure.

## Related

- [`runbooks/bad-release.md`](runbooks/bad-release.md) — what to do when a
  deploy regresses.
- [`runbooks/restore.md`](runbooks/restore.md) — recovery when a
  migration or deploy corrupts data.
