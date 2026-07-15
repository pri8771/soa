# Supply-chain security pipeline (SEC-011)

How this platform scans its dependencies, containers, and secrets; the
severity policy that turns findings into a release decision; how SBOMs
are produced; and how a genuinely-unfixable finding is accepted for a
limited, attributed window.

The gate is **code**, not a scanner's default exit code:
`scripts/supply_chain.py` (CLI) over
`packages/config/src/soa_config/supply_chain.py` (policy engine, unit
tested in `tests/security/test_supply_chain.py`). CI runs it in the
`security` job of `.github/workflows/ci.yml`.

## What is scanned

| Surface | Tool | When |
| --- | --- | --- |
| Python dependency closure | `pip-audit` (against the fully-resolved `uv export`) | every push / PR |
| JavaScript dependency closure | `pnpm audit --json` | every push / PR |
| Committed secrets | `gitleaks` (full history) | every push / PR |
| Software bill of materials | CycloneDX (`cyclonedx-py` for Python, `@cyclonedx/cyclonedx-npm` for JS) | every push / PR, uploaded as build artifacts |
| Container images | Trivy (image scan) | every push / PR, in the `container-images` job — see [Container scanning](#container-scanning) |

## Severity policy

A scanner finding maps to one of three outcomes:

- **Blocking** — `critical`, `high`, or **`unknown`** severity. The
  release is refused. `unknown` blocks because it is *fail-closed*:
  `pip-audit` does not score advisories, so every Python finding arrives
  unscored, and an advisory we cannot rate is treated as serious until
  proven otherwise. In practice this means **every Python advisory
  blocks** until it is fixed or explicitly excepted.
- **Informational** — `moderate` / `low`. Reported in the job log,
  never blocking. These come from `pnpm audit`, whose advisories carry a
  CVSS-derived severity (the engine reads both the legacy `advisories`
  schema pnpm currently prints and the newer npm `vulnerabilities` map).
- **Suppressed** — a blocking finding that an active exception names
  (see below). Reported, not blocking.

The acceptance criterion for SEC-011 — *unresolved critical findings
block release* — is enforced by this policy: `critical` is always
blocking and can only be silenced by a time-limited, attributed
exception.

## Exception process

Fix the dependency. Only when a finding genuinely **cannot** be fixed
right now — no upstream patch exists, or the affected code path is
provably unreachable — record a time-limited acceptance in
`security/vulnerability-exceptions.toml`:

```toml
[[exception]]
id = "GHSA-xxxx-xxxx-xxxx"   # advisory id OR any alias a scanner reports (CVE/GHSA)
ecosystem = "pypi"           # "pypi" or "npm"
package = "example-lib"
reason = "No fix released upstream; the affected parser API is never called by us."
owner = "security@northstar.example"
added = "2026-07-13"
expires = "2026-10-13"       # re-review required on or before this day
```

Rules the engine enforces:

- an exception suppresses a finding only when the **ecosystem** matches
  and its `id` equals the finding's id **or one of its aliases** — so a
  single entry covers the same advisory whether a scanner reports the
  CVE or the GHSA id;
- an **expired** exception fails CI on its own (`check-exceptions`),
  independent of whether the underlying finding still exists — accepted
  risk is renewed with a fresh review, never left to rot;
- the PR that adds an exception must justify, in its description, why
  the risk is acceptable for the stated window.

The committed registry ships **empty**: the platform carries zero
accepted vulnerabilities by default.

## Software bill of materials (SBOM)

Each CI run produces CycloneDX SBOMs for both dependency closures and
uploads them as the `sbom` build artifact (retained 90 days). Per
`docs/DELIVERY_PLAN.md`, a release pins its SBOM so the exact dependency
set behind any shipped version is recoverable and auditable.

## Container scanning

The `container-images` CI job builds each runtime image
(`apps/api/Dockerfile`, `apps/worker/Dockerfile`, `apps/web/Dockerfile`;
see [`CONTAINER_IMAGES.md`](CONTAINER_IMAGES.md)), asserts the runtime
user is **non-root**, and runs Trivy against each with a block-on-
`critical`/`high` policy.

Trivy runs with `ignore-unfixed: true`: an OS-package CVE with no
released fix cannot be remediated by us, so failing on it would only
force a noise exception. This differs deliberately from the dependency
gate, which fails closed — application dependencies we *can* pin or
upgrade get no such grace.

A prior unbounded `.trivyignore` was removed when the digest-pinned Python
base eliminated those findings. Container scans currently carry **no ignored
vulnerability IDs**. A future exception must use Trivy's structured YAML
ignore format with an owner/reason in `statement` and an `expired_at` date,
and must be reviewed like the dependency exceptions above. The runtime
sandbox profile those images must satisfy at deploy time (read-only root
filesystem, dropped capabilities, no network for converters) is specified in
[`SANDBOX_PROFILE.md`](SANDBOX_PROFILE.md).

## Running it locally

```
make security-scan
```

runs `pip-audit`, `pnpm audit`, the exception check, and the policy gate
exactly as CI does. The gate itself needs no project install — it is
pure standard library:

```
python3 scripts/supply_chain.py check-exceptions
python3 scripts/supply_chain.py evaluate --pip-audit pip-audit.json --pnpm-audit pnpm-audit.json
```
