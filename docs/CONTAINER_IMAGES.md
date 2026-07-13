# Container images (REL-001)

Three runtime images, all built from the repository root so the uv /
pnpm workspaces resolve:

| Image | Dockerfile | Base | Runs | Health |
| --- | --- | --- | --- | --- |
| `soa-api` | `apps/api/Dockerfile` | `python:3.11-slim-bookworm` | `uvicorn soa_api.main:app` on :8000 | `HEALTHCHECK` hits `/health/live` (stdlib, no curl) |
| `soa-worker` | `apps/worker/Dockerfile` | `python:3.11-slim-bookworm` + Tesseract + fonts | `soa-worker` | `HEALTHCHECK` runs `python -m soa_worker.healthcheck` against the heartbeat file |
| `soa-web` | `apps/web/Dockerfile` | `nginxinc/nginx-unprivileged:1.27-bookworm` | nginx on :8080 serving the Vite bundle | external HTTP probe on `/healthz` |

## Design

- **Multi-stage, nothing extra in runtime.** The Python images build a
  self-contained virtualenv with `uv sync --no-editable` in a builder
  stage; the runtime stage copies only `/app/.venv`. No `uv`, no
  compilers, no source tree, no dev dependencies. The web image builds
  with Node in a builder stage and ships only the static `dist/` under
  nginx.
- **Non-root.** The Python images run as a fixed system user (`soa`,
  uid 10001) with no home and no shell; the web image uses the
  unprivileged nginx base (uid 101). The `container-images` CI job
  asserts the runtime uid is not 0.
- **No secrets in the image.** `.dockerignore` keeps `.env*`, keys, and
  local state out of the build context; configuration is supplied at run
  time via `SOA_*` environment variables (secret *values* live behind
  the SEC-005 secret store, never baked in).
- **Pinned bases.** Base images are pinned to the Debian bookworm /
  Python 3.11 (and nginx 1.27, node 22) series — the latest patch, so
  Trivy scans stay clean; REL-002 tightens these to reproducible
  registry digests with tooling to refresh them.
- **Health checks.** The API reuses its own `/health/live` endpoint. The
  worker has no HTTP surface, so its liveness is a heartbeat file the run
  loop rewrites each beat (`SOA_WORKER_LIVENESS_FILE`, default
  `/tmp/soa-worker.heartbeat`); `soa_worker.healthcheck` fails when the
  file is missing or stale. The web image is intentionally toolless, so
  liveness is an external probe against the nginx `/healthz` location.

## Building locally

```
make docker-build
```

builds all three images, or individually from the repo root:

```
docker build -f apps/api/Dockerfile -t soa-api .
docker build -f apps/worker/Dockerfile -t soa-worker .
docker build -f apps/web/Dockerfile -t soa-web .
```

Image vulnerability scanning (Trivy) and the non-root assertion run in
CI; see [`SUPPLY_CHAIN_SECURITY.md`](SUPPLY_CHAIN_SECURITY.md). The
deploy-time runtime hardening these images must run under (read-only
root filesystem, dropped capabilities, seccomp, no network for
converters) is specified in [`SANDBOX_PROFILE.md`](SANDBOX_PROFILE.md)
and enforced by REL-002 infrastructure.
