# Running SOA locally (no Docker required)

This is the fastest way to run the whole product on one machine and process a
purchase order end to end — upload → extract → review → approve — using the
built-in deterministic mock extractor. **No Docker, MinIO, LLM, or OCR
required.** You need only Python 3.11, Node 22, pnpm, [uv](https://docs.astral.sh/uv/),
and a local PostgreSQL.

## One-time setup

```bash
make bootstrap          # install toolchains + copy .env
```

**PostgreSQL.** Either use Docker (`make local-up` starts Postgres + MinIO), or
run a native Postgres and create the app's role + database:

```bash
psql -h 127.0.0.1 -p 5432 -d postgres -c "CREATE ROLE soa_dev LOGIN SUPERUSER PASSWORD 'soa_dev_password';"
psql -h 127.0.0.1 -p 5432 -d postgres -c "CREATE DATABASE soa OWNER soa_dev;"
```

Then build the schema and seed a ready-to-use demo tenant:

```bash
make migrate            # create tables
make seed               # Northstar org + published process + active "uploads" stream
```

## Run the stack (three terminals)

The API and worker use **filesystem** object storage (local disk, shared
between them) so you don't need MinIO. The env vars below select it and allow
the browser origin.

**Terminal 1 — API:**
```bash
SOA_API_STORAGE_BACKEND=filesystem \
SOA_API_CORS_ALLOWED_ORIGINS='["http://localhost:5173"]' \
uv run uvicorn soa_api.main:app --port 8000
```

**Terminal 2 — worker** (claims jobs and runs the pipeline):
```bash
SOA_WORKER_STORAGE_BACKEND=filesystem \
uv run soa-worker
```

**Terminal 3 — web:**
```bash
VITE_API_BASE_URL=http://127.0.0.1:8000 \
pnpm --filter @soa/web run dev
```

Open **http://localhost:5173/app/northstar/overview**. In development the web
app auto-signs-in as the seeded demo admin (`admin@northstar.example`), so
there is no login step.

## Process a purchase order

1. Click **Documents → Upload documents**, pick the **uploads** stream, and
   upload a PDF (any PDF works; the mock extractor recognizes the built-in
   sample and returns absent-but-honest fields for anything else).
2. The worker claims the job and runs render → extract → normalize → validate.
3. The document appears in **Documents** and, depending on confidence, either
   lands as **approved** or creates a task in the **Review** queue, where you
   can correct fields against the document and approve it.

## Storage & filesystem notes

- `SOA_API_STORAGE_BACKEND=filesystem` stores object bytes under
  `.local-storage/` (git-ignored) and serves signed upload/download URLs from
  the API's `/_local-blobs` endpoint. The worker reads the same directory.
- The filesystem backend is **development-only**; settings validation refuses
  it in production (which uses S3/GCS).

## Optional: real extraction instead of the mock

The pipeline defaults to the deterministic mock provider, but the worker
honors `SOA_WORKER_EXTRACTION_PROVIDER` — set it to a registered provider name
to run a real model instead (a local LLM via Ollama, or a BYO Claude / Gemini /
OpenAI key). See [`docs/LLM_PROVIDERS.md`](LLM_PROVIDERS.md) for the provider
names and setup. Per-stream provider routing (so each stream can pick its own
provider) is still a follow-on; the value of `SOA_WORKER_EXTRACTION_PROVIDER`
is a single worker-level choice applied to every run.
