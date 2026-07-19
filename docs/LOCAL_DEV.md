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

## Run the stack (one command)

The API and worker use the same **filesystem** object store by default, so you
do not need MinIO. `make dev` (`python3 scripts/dev.py`) loads `.env`, applies
the no-Docker development defaults for all three services — including
`SOA_WORKER_LOCAL_LLM_ENDPOINT=http://localhost:11434/v1/chat/completions`,
`SOA_WORKER_LOCAL_LLM_MODEL=qwen2.5-coder:14b`, and
`SOA_WORKER_LOCAL_LLM_TIMEOUT_SECONDS=480` so the worker registers the local
Ollama adapter out of the box — starts all three services, and stops the
group if a child exits. An explicit shell export (or a value already set in
`.env`) always overrides these defaults, so pointing at a different model or
storage backend still works. Run `ollama serve` and `ollama pull
qwen2.5-coder:14b` first (see below) if you want documents to actually
extract with the local model instead of the deterministic mock; `make dev`
itself never requires Ollama to be running.

The three-terminal form below is useful for debugging one service in
isolation.

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

By default the seed pins the deterministic mock provider (so tests and CI are
self-contained). To seed a demo tenant whose documents extract with a REAL
local model instead, the pieces must line up in three places — the worker
registers the adapter, the seed pins it in the stream's provider policy, and
the model endpoint is actually reachable:

1. **Run a local model.** Install [Ollama](https://ollama.com), then:
   ```bash
   ollama serve &                       # must stay running
   ollama pull qwen2.5-coder:14b        # an instruction-following model
   ```
   (Small models like `gemma3:4b` read the document but ignore the schema and
   drop every field — use a 14B-class instruct/coder model or a BYO hosted key.)

2. **Seed the provider policy to name that provider.** The provider the pipeline
   uses is the one PINNED in the stream's provider policy — a worker env var
   registers an adapter but never overrides a published policy. Re-seed with the
   override so the policy names the local adapter:
   ```bash
   SOA_SEED_EXTRACTION_PROVIDER=local-openai-compatible make seed
   ```
   (Defaults to `mock` when unset. Re-seeding needs a clean schema:
   `uv run alembic downgrade base && uv run alembic upgrade head` first.)

3. **Register the adapter in the worker** by pointing it at the endpoint:
   ```bash
   SOA_WORKER_STORAGE_BACKEND=filesystem \
   SOA_WORKER_LOCAL_LLM_ENDPOINT=http://localhost:11434/v1/chat/completions \
   SOA_WORKER_LOCAL_LLM_MODEL=qwen2.5-coder:14b \
   SOA_WORKER_LOCAL_LLM_TIMEOUT_SECONDS=480 \
   uv run soa-worker
   ```
   Local models on shared hardware can take minutes per document; the generous
   timeout keeps the first (cold-load) call from being cut off.

   The worker processes one job at a time by default
   (`SOA_WORKER_MAX_CONCURRENCY=1`), so a 45-250s LLM call serializes the
   whole queue behind it. Add `SOA_WORKER_MAX_CONCURRENCY=4` (range 1-32) to
   the command above to process documents in parallel; staging/production set
   this via the `worker_concurrency` Terraform variable (see
   [`INFRASTRUCTURE.md`](INFRASTRUCTURE.md)).

**Gotcha — a down endpoint poisons routing.** If the model endpoint is
unreachable when a document runs, the provider records consecutive failures in
`provider_runtime_metrics` and the AIO-013 router then eliminates it as
`unreachable` — so even after the endpoint is back, routing raises
`NoRouteError` until the stale health clears. There is no automatic re-probe
yet; clear it manually after fixing the endpoint:
```bash
psql "$SOA_DATABASE_URL" -c \
  "DELETE FROM provider_runtime_metrics WHERE provider='local-openai-compatible';"
```

See [`LLM_PROVIDERS.md`](LLM_PROVIDERS.md) for hosted BYO-key providers.
