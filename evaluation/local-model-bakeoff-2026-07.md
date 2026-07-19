# Local-model bake-off — July 2026

**Status: pulls and smoke tests complete; no stream pins changed.** This
document is honest about what was and was not measured — see
[`README.md`](README.md) for why the repository's only committed gold
corpus cannot establish extraction accuracy either.

## What was done

1. **Pulled and smoke-tested all three candidates** against the local
   Ollama endpoint (`http://localhost:11434/v1/chat/completions`):

   | Model | Size on disk | Smoke test |
   | --- | --- | --- |
   | `qwen3:30b` (Qwen3-30B-A3B MoE) | 18 GB | Pass — valid `{"ok":true}` |
   | `gemma3:27b` | 17 GB | Pass — valid JSON (wrapped in a ` ```json ` fence) |
   | `mistral-small3.2` | 15 GB | Pass — valid JSON (wrapped in a ` ```json ` fence) |

   `gemma3` and `mistral-small3.2` wrap JSON in a markdown code fence even
   when asked not to; SOA's AIO-012 schema-repair path already handles
   this class of malformed-but-recoverable output, so it is not
   disqualifying on its own, but it does mean a real run would show a
   nonzero repair-fallback count for these two where `qwen2.5-coder:14b`
   (the current pin) shows none.

2. **Did not run the server-executed evaluation against the seeded gold
   sets**, for two independent reasons discovered while trying to:

   - **The gold set is too thin to decide anything.** The `uk` stream
     (org `northstar`) has three gold datasets seeded by the
     extraction-training feature: `uk-live-samples` (1 published
     document), `uk-annotate-test` (1 document, draft/unpublished), and
     `uk-heldout` (2 published documents). A held-out accuracy
     comparison needs more than 2 documents per candidate to mean
     anything — a single document flipping right-or-wrong swings the
     "accuracy" by 50 percentage points. `spain` has no seeded gold
     dataset at all. This matches the documented state in
     [`README.md`](README.md): the only *committed* corpus in this repo
     is a synthetic, non-executable manifest fixture, explicitly not
     usable to establish accuracy.

   - **Model choice is not a stream-policy field.** The task brief
     assumed "the per-stream pinned policy is authoritative" for model
     selection, matching how `local-openai-compatible` is described
     elsewhere in the docs. The actual provider-policy schema
     (`apps/api/src/soa_api/domain/policies.py`) validates only
     `provider_name` (plus fallback provider name/credential-reference
     fields) — there is no `model` field. The model actually used by the
     `local-openai-compatible` adapter is fixed at **worker startup**
     from `SOA_WORKER_LOCAL_LLM_MODEL`
     (`register_local_llm_extraction()` in
     `apps/worker/src/soa_worker/llm_extraction.py`), not from the
     versioned policy content. Running a real head-to-head server
     evaluation for the three candidates therefore requires restarting
     the (shared, currently live) dev worker with a different env var
     between each candidate run, not just creating draft stream
     versions — a heavier and more disruptive operation than the task
     assumed, for a result that a 2-document gold set could not make
     trustworthy anyway.

   Per the task brief's own fallback ("if the evaluation flow needs data
   that doesn't exist... write up what exists, note the blocker, and
   move on — do NOT fabricate benchmark numbers"), this is that
   write-up. **No draft provider policies were created** (there is
   nothing for the policy layer to hold — see above), and **no stream
   pins were changed.**

## Supplementary signal (informal, not promotion evidence)

To get *some* real, non-fabricated signal instead of none, one
synthetic English purchase order and one synthetic Spanish purchase
order (hand-written for this check, not from the gold set) were sent to
all four models — the current pin plus the three candidates — through
the exact `local-openai-compatible` request shape (temperature 0, one
user turn, a JSON-object instruction). This is a single-document smoke
timing, not an accuracy measurement: all four models extracted every
field correctly on both documents, so the only thing this
differentiates is latency and completion verbosity.

| Model | English PO | Spanish PO | Completion tokens (EN) |
| --- | --- | --- | --- |
| `qwen2.5-coder:14b` (current pin) | 14.9s | 12.3s | 250 |
| `gemma3:27b` | 23.5s | 28.3s | 252 |
| `mistral-small3.2` | 25.0s | 27.3s | 286 |
| `qwen3:30b` | 27.8s | **timed out (>120s)** | 1,890 |

`qwen3:30b` is a reasoning model that emits a long chain-of-thought
before its answer by default (1,890 completion tokens for a 250-token
answer on the English document, and it did not finish the Spanish
document within the check's timeout at all). Suppressing that
(`/no_think` or an equivalent `options.think: false`, if the Ollama
build supports it for this model) was not attempted here — pursuing it
is exactly the kind of tuning that belongs in a real evaluation run, not
a bake-off write-up. As pulled and run with default settings, `qwen3:30b`
is the **worst** latency result of the four, not the "expected best
speed/quality trade-off" the task brief anticipated.

## Recommendation

Do not change the `uk` or `spain` provider policy pins. Before any real
promotion decision:

1. Seed a larger held-out gold set for both streams (aim for the same
   order of magnitude as a real pilot cohort, not 2 documents) — this is
   the same corpus gap `evaluation/README.md` already flags as a
   production blocker (§6, item 7 in the parent task list).
2. If model-level A/B testing across candidates becomes a recurring
   need, add a `model` field to the `local-openai-compatible` policy
   schema so a published stream version can pin a specific model the way
   it already pins `provider_name` — today that requires a worker
   restart per candidate, which does not compose with running several
   candidates against a live shared dev worker.
3. Re-run `qwen3:30b` with reasoning/thinking suppressed before judging
   it on latency; the number above is not representative of what a
   tuned configuration would look like.

## Models available for a future run

All three candidates are pulled and confirmed working on this machine
(`ollama list`): `qwen3:30b`, `gemma3:27b`, `mistral-small3.2`, alongside
the existing `qwen2.5-coder:14b` pin. No cleanup was performed — they
remain available locally for whoever picks this up next.
