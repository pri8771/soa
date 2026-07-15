# LLM extraction providers — local and BYO key (AIO-007/008)

How the platform runs field extraction with a **local** model and,
optionally, with a customer's **own** Claude / Gemini / OpenAI key.
Every option plugs into the same AIO-001 capability contract, is built by
the same AIO-011 injection-safe request builder, and can be scored by the
same server-executed gold-dataset evaluation (AIO-016/017). Switching models
or configuring an ordered fallback is a versioned policy change; combining
outputs is not implemented.

## The one rule that governs "which model"

Extraction never treats a model's answer as proof. Confidence is the model's
**own uncalibrated self-report** (every result carries a warning), and the
current text model has **no geometry**, so its evidence is page-level only.
Deterministic normalization, catalogs, rules, evidence, field criticality, and
the calibrated review policy decide whether a result may advance.

The router supports ordered fallback after retryable/provider-contract
failure; it is not an ensemble and it does not infer correctness from model
agreement. Add adjudication or a second quality pass only when a gold-corpus
experiment shows a failure class the first path misses and measures the added
latency/cost.

The current runtime sends recognized page **text** to the extraction provider:
native PDF text where usable, otherwise bounded Tesseract output. It does not
send page images to a multimodal model. Do not claim vision-model quality until
a bounded page-image/evidence path exists and wins representative evaluation.

## Recommended local stack

Run these with [Ollama](https://ollama.com), which exposes an
OpenAI-compatible `/v1/chat/completions` endpoint — exactly what the
AIO-007 adapter speaks.

| Runtime role | Starting point | Current behavior |
| --- | --- | --- |
| Text → JSON extraction | **Qwen2.5-7B-Instruct** | Receives bounded native/OCR text through the OpenAI-compatible adapter. This is an evaluation candidate, not a universal quality claim. |
| OCR for scanned pages | **Tesseract** | Produces the text supplied to the extraction model; local and no third party. |
| Future multimodal candidate | **Qwen2.5-VL-7B-Instruct** | Not wired to page images today; evaluate only after the request/evidence contract exists. |

Capacity depends on quantization, context size, serving engine, concurrency,
and hardware. Measure memory, latency, and accuracy in the target environment;
do not infer production capacity from parameter count alone. Move to a larger
model only when the corpus evaluation identifies the model as the bottleneck.

### Running it

```bash
# 1. Install and start Ollama, then pull the current text candidate
ollama pull qwen2.5:7b-instruct

# 2. Point the worker at the local endpoint (fail-closed: unset = no
#    local provider). Ollama serves the OpenAI-compatible API on :11434.
export SOA_WORKER_LOCAL_LLM_ENDPOINT="http://localhost:11434/v1/chat/completions"
export SOA_WORKER_LOCAL_LLM_MODEL="qwen2.5:7b-instruct"

# 3. Optional: raise the per-call answer budget (default 60s). Local
#    models on shared hardware can need minutes on a long document.
export SOA_WORKER_LOCAL_LLM_TIMEOUT_SECONDS="240"
```

The local provider registers with the **strict local data policy**: content
never leaves the deployment and the adapter does not retain it. Registering an
adapter does not select it globally; every run executes the provider policy
published for that stream. A local-only policy excludes hosted candidates.

## Bring-your-own hosted key (optional)

A deployment may also register a hosted model factory. Production runs resolve
the exact customer-supplied secret reference pinned in the stream/run. Hosted
providers declare honestly that they **send content to a third party**, so the
router (AIO-013) only ever selects one when the
resolved tenant policy explicitly allows third-party processing — a
local-only tenant is never silently sent to a cloud model.

Every hosted invocation is **fail-closed**: the run-pinned tenant credential
is authoritative when present, and an empty/invalid tenant value never falls
through to a shared deployment key. With neither a tenant nor deployment
credential, provider construction fails before document content is sent.

### Claude (Anthropic Messages API)

Claude is the one provider with its own adapter — the Messages API
differs enough from OpenAI's to warrant it. It reuses the same
injection-safe builder and the same output parser as every other model
adapter.

```bash
export SOA_WORKER_ANTHROPIC_API_KEY="sk-ant-..."
export SOA_WORKER_ANTHROPIC_MODEL="claude-sonnet-4-5"   # default; overridable
```

### Gemini (native)

A native Gemini adapter (AIO-009) speaks Google's `generateContent` API
directly — a third API shape alongside Claude's Messages and OpenAI's
chat-completions, which is what proves the provider contract is portable.
The key travels as an `x-goog-api-key` header.

```bash
export SOA_WORKER_GEMINI_API_KEY="AIza-..."
export SOA_WORKER_GEMINI_MODEL="gemini-2.0-flash"   # default; overridable
```

### Gemini and OpenAI (OpenAI-compatible endpoint)

Both also expose an OpenAI-compatible `/chat/completions` endpoint, so as
an alternative they can reuse the AIO-007 adapter with an endpoint + key.
The key travels as a `Bearer` token; the provider registers under a name
you choose so an operator can tell them apart.

```bash
# OpenAI
export SOA_WORKER_HOSTED_OPENAI_API_KEY="sk-..."
export SOA_WORKER_HOSTED_OPENAI_ENDPOINT="https://api.openai.com/v1/chat/completions"
export SOA_WORKER_HOSTED_OPENAI_MODEL="gpt-4o"
export SOA_WORKER_HOSTED_OPENAI_PROVIDER_NAME="hosted-openai"
export SOA_WORKER_HOSTED_OPENAI_REGION="us"

# Gemini (its OpenAI-compatible endpoint) — same knobs, different values
export SOA_WORKER_HOSTED_OPENAI_API_KEY="AIza..."
export SOA_WORKER_HOSTED_OPENAI_ENDPOINT="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
export SOA_WORKER_HOSTED_OPENAI_MODEL="gemini-2.0-flash"
export SOA_WORKER_HOSTED_OPENAI_PROVIDER_NAME="hosted-gemini"
```

These variables register deployment capabilities. Provider selection is part
of the published stream policy (`provider_name` plus any pinned fallback chain
and tenant credential references), so a deployment variable cannot silently
select a different provider for a run. The adapter endpoint, model, timeout,
and token budget are deployment-runtime inputs rather than control-plane pins;
a worker restart can change them for a queued retry. All three model-adapter
shapes declare a normalized, credential-free descriptor containing adapter,
model, endpoint SHA-256, timeout, maximum output tokens, and temperature.
OpenAI-compatible calls additionally record their fixed seed; Anthropic does
not expose a seed, and the native Gemini adapter does not currently send one.
Hosted descriptors also retain the configured rate card, while Anthropic
retains its API version.

Before every routed adapter invocation, including a bounded repair attempt,
the worker commits that safe descriptor and its fingerprint to the running
extraction stage and appends a `provider.call_started` audit event. A call
therefore cannot begin without durable evidence. The event is intentionally a
call-start intent, not proof that the vendor received the request: a process
can crash after the commit and before HTTP I/O. A successful run's runtime
provenance and fingerprint include the complete attempted provider chain and
the selected adapter. If every provider fails, the failed stage retains the
pre-call descriptors even though the run has no successful extraction runtime
fingerprint. Reproducibility checks must compare successful runtime provenance
as well as the immutable execution pin.

> The environment-variable keys above are the simplest path for local
> testing (one deployment-level key). Production stream policies pin
> **per-tenant** BYO keys through the SEC-005 secret store
> (`secretref://…`, never the raw value in the database); the worker resolves
> only that pinned reference for the run. Deployment-level keys remain useful
> for local/evaluation profiles; production run resolution never substitutes
> one for a missing tenant credential pin. Provider terms, region,
> retention/training behavior, and customer authorization still require live
> review before enabling a hosted candidate.

### Managed credential lifecycle

Credential managers enter a provider API key through the write-only
administration route/UI. The value goes directly to the configured secret
store; database/API state contains safe metadata and an internal reference, and
the value/reference is never redisplayed. Provider policy drafts submit the
credential metadata ID, not `credential_ref`; the server binds and validates
the reference before storing/publishing.

Rotation creates a new current credential but retains the superseded secret so
already-published immutable policies and in-flight run pins remain reproducible.
Normal revocation is refused while any policy references the credential.
Audited force revocation is break-glass and deliberately makes affected pins
fail closed. The database session registers an idempotent external-secret
revoke as rollback compensation immediately after a new value is written, so a
later database rollback does not orphan that value. The managed provider
credential type is currently `api_key`; OAuth refresh/token flows need a
separate credential implementation.

## Token usage and estimated provider cost

Hosted adapters fail safe unless a successful response contains valid token
usage. They preserve the provider-reported input, output, and total counts;
Anthropic does not report a total, so its total is explicitly derived as input
plus output. The immutable extracting-stage summary retains the full breakdown,
and the usage ledger records one row per provider call with
`billed_unit=tokens` and the reported total. Repair attempts and routed
fallbacks retain separate provider/model/rate-card attribution; terminal calls
are still ledgered before the stage fails when the adapter can report their
usage. Local OpenAI-compatible responses may report the same facts, but their
provider cost remains zero; the deterministic mock remains a zero-cost call.

Cost is an estimate, separate from those facts. Each worker deployment pins a
named rate card and integer cents per one million input/output tokens. The
calculation combines both components and rounds a non-zero fractional-cent call
up once to fit the ledger's integer-cent representation. No adapter looks up a
vendor price at runtime.

The built-in values are static starter estimates for the default model names,
not claims about current public prices:

| Provider profile  | Input cents / 1M | Output cents / 1M | Default reference                                  |
| ----------------- | ---------------- | ----------------- | -------------------------------------------------- |
| Anthropic Claude  | 300              | 1,500             | `soa-rate-card-v1:anthropic:claude-sonnet-4-5`     |
| Gemini native     | 10               | 40                | `soa-rate-card-v1:google:gemini-2.0-flash`         |
| OpenAI-compatible | 250              | 1,000             | `soa-rate-card-v1:openai-compatible:gpt-4o`        |

Before enabling a hosted provider in production, confirm the contracted rates
and set the matching `SOA_WORKER_<PROVIDER>_PRICING_REFERENCE`,
`..._INPUT_CENTS_PER_MILLION`, and `..._OUTPUT_CENTS_PER_MILLION` values. Change
the reference whenever either rate or model changes. Provider invoices remain
the settlement source of truth: reconcile differences by appending an audited
usage-ledger adjustment instead of rewriting the original token facts.

A timeout can occur after a vendor accepted work but before it returned usage.
That call is retained as `provider_usage=unreported` with a safe stage warning;
the router reserves its configured estimate against the document budget, but
does not invent token counts or claim that a zero recorded estimate proves a
zero invoice charge. Invoice reconciliation must append the eventual delta.

## Safety, identical across every provider

- **No tools, ever.** No adapter sends a `tools` field — the model reads
  a document, it never acts. A test asserts this structurally for each.
- **Injection-safe request.** System instructions and document text are
  separated; document text travels only as JSON string values; tenant
  instructions containing URLs are refused (AIO-011).
- **Honest output.** Unrequested fields are dropped with a warning;
  absent fields return `null` with no evidence; confidence is clamped to
  `[0, 1]` and flagged as a self-report.
- **Bounded repair.** Malformed model output is re-asked a bounded number
  of times with a safe reason, then routed to review (AIO-012) — never an
  infinite loop, and model output never leaks into logs or error
  messages.

## How a request picks a provider

The router (AIO-013) chooses among the registered providers by
capability, language, region, **data policy** (local-only vs
third-party-allowed), budget, and health — with a deterministic
explanation. Configure the preference per stream through the CFG-005
provider policy; the eval promotion gate (AIO-017) guards any change
before it reaches production.

The published policy may pin an ordered extraction chain. Existing
single-provider policies remain valid; fallbacks are opt-in and every hosted
entry carries its own immutable secret reference:

```json
{
  "provider_name": "local-openai-compatible",
  "capabilities": ["ocr", "field_extraction"],
  "estimated_cost_cents": 0,
  "evaluated_quality_score": 0.91,
  "fallback_providers": [
    {
      "provider_name": "anthropic-claude",
      "credential_id": "8d425095-a216-4f50-a53c-5e520257f9b4",
      "estimated_cost_cents": 4,
      "evaluated_quality_score": 0.94
    }
  ],
  "allow_third_party_processing": true,
  "allowed_regions": ["us"],
  "budget_cents": 6,
  "min_quality": 0.9
}
```

Administrators submit the opaque credential metadata ID; API reads never expose
the value or its secret-store reference. The server verifies that the
credential is current, live, tenant-owned, and for the named provider, then
binds its immutable `secretref://` value into the stored policy/run snapshot.

At run time the worker authenticates the exact policy version and primary
credential pin, resolves every candidate credential through the tenant secret
store, and refuses unregistered adapters or data-policy/region violations. A
retryable provider failure (or adapter output that violates the extraction
contract) may advance to the next pinned provider; terminal adapter/request
failures do not. Cumulative estimates and actual paid failed calls count
against the document budget, so fallback cannot silently overspend it.

A non-zero `min_quality` is satisfied only by each candidate's immutable
`evaluated_quality_score`. Populate that score from the corresponding
server-side gold evaluation; the stream promotion gate independently blocks an
untested policy change. Live model confidence remains visible as telemetry but
cannot stand in for measured accuracy; a candidate without a pinned evaluated
score is excluded when a quality floor applies.

Routing explanations and selected/fallback providers are append-only audit
events. Each call-start event contains only the allowlisted adapter descriptor;
unknown adapter keys are dropped so an extension cannot add a credential to
durable provenance accidentally. After an outcome, each provider attempt also
updates tenant-scoped health, latency, confidence, safe failure-class,
fallback, and cost aggregates shared by all worker replicas and shown in the
provider administration API. No document content, vendor response, credential
value, or raw error is stored in those metrics.
