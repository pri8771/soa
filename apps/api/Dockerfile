# SOA API runtime image (REL-001).
#
# Multi-stage: a builder resolves the locked dependency closure into a
# self-contained virtualenv; the runtime stage copies only that venv onto
# a slim base. No uv, no compilers, no source tree, and no secrets survive
# into the final image — just Python and the installed packages, run as an
# unprivileged user. Build from the REPO ROOT (the uv workspace root):
#   docker build -f apps/api/Dockerfile -t soa-api .
#
# Base images are pinned to the Debian bookworm / Python 3.11 series (the
# latest patch, so image scans stay clean); REL-002 tightens these to
# reproducible registry digests with tooling to refresh them.

FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS builder

# Pinned uv, copied from its published image (no curl|sh install).
COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY . .
# Install only the API package + its workspace deps, no dev group, and
# NON-editable so the venv carries the code and the source tree can be
# discarded with the builder stage.
RUN uv sync --frozen --no-dev --no-editable --package soa-api


FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS runtime-base

# The upstream Python image includes packaging tools for general-purpose use.
# This image installs its immutable environment in the builder, so retaining
# pip/setuptools/wheel at runtime only adds unused code and vulnerability
# surface. Uninstall them before copying the already-built virtualenv.
RUN python -m pip uninstall --yes wheel setuptools pip

# Drop root: a fixed, high, system UID with no shell and no home writes.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin soa

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder --chown=soa:soa /app/.venv /app/.venv

USER soa
WORKDIR /app


# One-shot migration image. It intentionally shares the exact locked Python
# environment with the API, but includes only the Alembic configuration and
# migration scripts in addition to that environment. Deployments promote this
# image by its own digest and run it with SOA_DATABASE_URL; the API runtime
# remains free of schema-management files and permissions.
FROM runtime-base AS migrator

COPY --chown=soa:soa alembic.ini /app/alembic.ini
COPY --chown=soa:soa migrations /app/migrations

CMD ["alembic", "upgrade", "head"]


FROM runtime-base AS api

EXPOSE 8000

# Liveness via the app's own endpoint using stdlib only (no curl in the
# image). A non-2xx or connection error exits non-zero → unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status==200 else 1)"]

CMD ["uvicorn", "soa_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
