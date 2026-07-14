# LLM extraction providers — local and BYO key (AIO-007/008)

How the platform runs field extraction with a **local** model by default
and, optionally, with a customer's **own** Claude / Gemini / OpenAI key.
Every option plugs into the same AIO-001 capability contract, is built by
the same AIO-011 injection-safe request builder, and is scored by the
same gold-dataset evaluation (AIO-016/017) — so switching or combining
models is a configuration change, never a code change.

## The one rule that governs "which model"

Extraction never trusts a single model's word. A model's confidence is
its **own uncalibrated self-report** (every result carries a warning
saying so), and a language model has **no geometry**, so evidence is
page-level only. The decision to auto-approve or send to human review is
the confidence policy's (PRC-011), informed by the gold evaluation — not
the model's. That is what makes "combine models for accuracy" safe:
agreement at high confidence auto-approves, disagreement routes to
review, and the eval measures whether a given model (or combination)
actually earns a higher auto-approve threshold.

**Do not hand-roll ensembling.** Add a second model only when the eval
shows a failure class the first one misses. Start with one good
vision-language model plus an OCR text fallback.

## Recommended local stack

Run these with [Ollama](https://ollama.com), which exposes an
OpenAI-compatible `/v1/chat/completions` endpoint — exactly what the
AIO-007 adapter speaks.

| Role                          | Model                        | Why |
| ----------------------------- | ---------------------------- | --- |
| Primary vision extractor      | **Qwen2.5-VL-7B-Instruct**   | Best open document-extraction quality at 7B; reads the page image directly, so it handles scans and complex layouts. |
| Text → JSON extractor         | **Qwen2.5-7B-Instruct**      | Cheaper, no image; for native-PDF text (AIO-002) or OCR'd text (AIO-004). |
| OCR (scanned pages)           | **Tesseract** (already built)| Feeds the text model; local, no third party. |

Hardware: the 7B models run on a single 16 GB GPU (or Apple Silicon with
≥16 GB unified memory); CPU-only works but is slow. Step up to a 32B/72B
Qwen variant only if the eval shows the 7B model is the bottleneck — the
router and eval make that an evidence-based swap.

### Running it

```bash
# 1. Install and start Ollama, then pull the models
ollama pull qwen2.5:7b-instruct
ollama pull qwen2.5vl:7b            # vision-language, for scanned/complex pages

# 2. Point the worker at the local endpoint (fail-closed: unset = no
#    local provider). Ollama serves the OpenAI-compatible API on :11434.
export SOA_WORKER_LOCAL_LLM_ENDPOINT="http://localhost:11434/v1/chat/completions"
export SOA_WORKER_LOCAL_LLM_MODEL="qwen2.5:7b-instruct"
```

The local provider registers with the **strict local data policy**:
content never leaves the deployment, nothing is retained. That is the
default and the only option a tenant with a local-only retention policy
will accept.

## Bring-your-own hosted key (optional)

A deployment may also register a hosted model with a customer-supplied
key. Hosted providers declare honestly that they **send content to a
third party**, so the router (AIO-013) only ever selects one when the
resolved tenant policy explicitly allows third-party processing — a
local-only tenant is never silently sent to a cloud model.

Every hosted key is **fail-closed**: no key configured means the provider
does not exist. You add a capability by adding a key, never by touching
code.

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

> The environment-variable keys above are the simplest path for local
> testing (one deployment-level key). **Per-tenant** BYO keys belong in
> the SEC-005 secret store (`secretref://…`, never the raw value in the
> database) and are resolved per tenant at request time; wiring the
> hosted adapters to per-tenant secret references is the follow-on
> (tracked with AIO-006 credential availability). Until then, a
> deployment-level key applies to the whole deployment.

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
