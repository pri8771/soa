# SOA worker runtime image (REL-001).
#
# Same multi-stage shape as the API image, plus the worker's runtime
# system dependencies: the Tesseract OCR engine and its language packs
# (AIO-004) and base fonts for faithful PDF rasterization (PRC-004).
# pypdfium2 and Pillow ship as self-contained wheels, so no compilers are
# needed at runtime. Build from the REPO ROOT:
#   docker build -f apps/worker/Dockerfile -t soa-worker .

FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev --no-editable --package soa-worker


FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS runtime

# OCR engine + the language packs the Tesseract adapter maps
# (soa_worker.tesseract_ocr.LANGUAGE_PACKS) and base fonts for rendering.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-deu \
        tesseract-ocr-fra \
        tesseract-ocr-spa \
        tesseract-ocr-ita \
        tesseract-ocr-nld \
        tesseract-ocr-por \
        tesseract-ocr-pol \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin soa

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOA_WORKER_LIVENESS_FILE=/tmp/soa-worker.heartbeat

COPY --from=builder --chown=soa:soa /app/.venv /app/.venv

USER soa
WORKDIR /app

# The worker has no HTTP surface; liveness is the heartbeat file the run
# loop rewrites each beat (soa_worker.healthcheck checks its freshness).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-m", "soa_worker.healthcheck"]

CMD ["soa-worker"]
