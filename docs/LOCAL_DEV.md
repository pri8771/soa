# Running SOA locally (Docker optional)

This is the fastest way to run the whole product on one machine and process a
purchase order end to end — upload → extract → review → approve — using the
built-in deterministic mock extractor. **No Docker, MinIO, hosted model, or
OCR engine is required.** You need Python 3.11, Node 22, pnpm,
[uv](https://docs.astral.sh/uv/), and PostgreSQL.

## One-time setup

```bash
make bootstrap          # install toolchains + copy .env
```

**PostgreSQL.** Either use Docker (`make local-up` starts PostgreSQL, MinIO, and
Mailpit), or run native PostgreSQL. The owner/migrator role applies DDL; API and
worker use the separate non-superuser `soa_app` runtime role so local RLS and
privilege behavior match production:

```bash
psql -h 127.0.0.1 -p 5432 -d postgres -c "CREATE ROLE soa_dev LOGIN SUPERUSER PASSWORD 'soa_dev_password';"
psql -h 127.0.0.1 -p 5432 -d postgres -c "CREATE DATABASE soa OWNER soa_dev;"
psql -h 127.0.0.1 -p 5432 -d soa -c "CREATE ROLE soa_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'soa_app_password';"
psql -h 127.0.0.1 -p 5432 -d soa -c "GRANT CONNECT ON DATABASE soa TO soa_app;"
```

Those commands are one-time setup and will report that an object exists if
repeated. The committed Compose bootstrap creates `soa_app` automatically.
Never copy the development superuser pattern to a deployed environment: the
reference deployment has a dedicated DDL migrator and strips elevated
`cloudsqlsuperuser` membership from the runtime account in migration `0045`.

Then build the schema and seed a ready-to-use demo tenant:

```bash
make migrate            # create tables
make seed               # Northstar org + published process + active "uploads" stream
```

`make migrate` reads `SOA_DATABASE_URL` (the owner/migrator URL). API, worker,
and the development seed read their `SOA_API_DATABASE_URL` /
`SOA_WORKER_DATABASE_URL` runtime URLs from `.env`.

## Run the stack (three terminals)

The API and worker use the same **filesystem** object store by default, so you
do not need MinIO. `make dev` loads `.env`, applies those development defaults,
starts all three services, and stops the group if a child exits. The three
terminal form below is useful for debugging one service.

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

## Optional local services

The default Compose profile starts PostgreSQL, MinIO, and Mailpit. Opt-in
profiles add slower or more resource-intensive dependencies:

```bash
docker compose -f infrastructure/local/docker-compose.yml --profile scanner up -d --wait
docker compose -f infrastructure/local/docker-compose.yml --profile telemetry up -d --wait
```

ClamAV is mandatory outside development/test. The local no-op scanner exists
only to keep this fast path small; it must never be treated as malware-scan
evidence.

## Optional: real extraction instead of the mock

The seeded stream pins the deterministic mock provider. To use a real model,
run a local LLM (for example Ollama) or configure a hosted BYO credential, then
publish a stream/provider-policy version that names that registered provider.
The worker authenticates and executes the exact policy and credential pinned to
the run; setting an environment variable registers an adapter but does not
silently override a published stream. See
[`LLM_PROVIDERS.md`](LLM_PROVIDERS.md).
