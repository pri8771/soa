# test-fixtures

Deterministic synthetic Northstar tenant records and adversarial text used by
local seeding, security tests, pipeline tests, and evaluation machinery.
Fixture UUIDs derive from stable aliases, and `SeedRunner` is idempotent over
any `SeedSink`.

The document fixtures contain metadata, recognized text, and expected fields;
their filenames do **not** point to committed PDF/image source bytes. They are
useful for deterministic behavior tests, not production extraction-accuracy
evidence. See the [evaluation fixture contract](../../evaluation/README.md) and
[production-readiness audit](../../docs/PRODUCTION_READINESS_AUDIT.md) before
using a dataset for promotion.

The database-backed application seed is implemented by
`soa_api.ops.seed` and exposed as `make seed`.
