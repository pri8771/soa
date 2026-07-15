# test-fixtures

Deterministic synthetic Northstar tenant fixtures used by local seeding,
security tests, pipeline tests, and offline evaluation. Fixture UUIDs derive
from stable aliases, and `SeedRunner` is idempotent over any `SeedSink`.

The database-backed application seed is implemented by
`soa_api.ops.seed` and exposed as `make seed`.
