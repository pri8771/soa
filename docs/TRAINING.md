# Extraction training (few-shot from labelled samples)

SOA extracts fields with an LLM, so "training" here is **curated few-shot
exemplars with optional positional hints — not rigid template zones**. A user
uploads a batch of sample documents, highlights the fields on each (draw a box →
assign it to a field → the enclosed text becomes the expected value), and those
labelled samples are fed back into extraction as worked examples and used to
measure the accuracy lift.

The feature is scoped **per stream** (per customer is a later step). It reuses,
rather than replaces, the platform's existing seams: the versioned gold-dataset
store, the injection-safe request builder, the per-stream instruction versions,
and the gold-set evaluation engine + promotion gate.

## The three phases at a glance

1. **Label** — create a *training set* on a stream, add processed sample
   documents, and annotate their fields (header + line items) into ground truth
   with per-field regions. Publish to freeze the set.
2. **Train** — compile the published set's `train`-split samples into few-shot
   exemplars and publish them as the stream's live extraction instructions.
   Extraction then carries those exemplars.
3. **Measure** — evaluate a config against the set's held-out (`validation` /
   `test`) slice; the report gives per-field accuracy, and a regression gate
   blocks a config that regresses critical fields from being promoted live.

## Data model

A **training set is a `gold_dataset` scoped to a stream** — `gold_datasets`
gained a nullable `stream_id` (migration `0054`). Org-level evaluation datasets
keep it `NULL`; a training set has it set. Everything else is the existing
gold-dataset machinery:

- `gold_datasets` → `gold_dataset_versions` (draft → published → superseded,
  one published at a time, immutable once published) → `gold_documents`.
- A `gold_document` references its tenant source document by
  `source_document_id` + `document_sha256`, carries a `split`
  (`train`/`validation`/`test`), and a **closed** `ground_truth` shape:

  ```json
  {
    "fields": { "po_number": "PO-4711", "currency": "EUR", "ship_date": null },
    "lines":  [ { "sku": "WIDGET-9", "quantity": "5" } ],
    "validations": [ "totals_match" ],
    "regions": {
      "po_number":     { "page_number": 1, "polygon": [[x,y],[x,y],[x,y],[x,y]] },
      "lines.0.sku":   { "page_number": 1, "polygon": [[x,y],...] }
    }
  }
  ```

  `fields`/`lines` are pure value maps (so the evaluation scorer is untouched);
  `regions` is the new, optional, bounded positional-hint slot. A header field
  keys by its plain key; a line-item cell keys as `lines.<row_index>.<column>`.
  Regions are raster-pixel polygons in the same coordinate system as extraction
  evidence, so no conversion is needed.

## Phase 1 — label

Samples are **ordinary documents uploaded to the stream** through the normal
pipeline (so they carry page images + positioned text). Labelling reuses the
Review Studio's `DocumentViewer` draw/assign surface. The API (`streams.manage`
to write, `streams.read` to read):

| Method | Path | Purpose |
|---|---|---|
| POST | `/orgs/{org}/streams/{stream}/training-sets` | create a training set (draft v1) |
| GET | `/orgs/{org}/streams/{stream}/training-sets` | list the stream's sets |
| GET/PATCH/DELETE | `…/training-sets/{slug}` | detail / rename / delete (draft-only) |
| POST | `…/training-sets/{slug}/versions` | open a new draft (clones the last version) |
| POST | `…/training-sets/{slug}/publish` | freeze the working draft |
| PUT | `…/training-sets/{slug}/documents` | upsert one sample's labels (idempotent) |
| GET | `…/training-sets/{slug}/documents` | list labelled samples |
| DELETE | `…/training-sets/{slug}/documents/{gold_id}` | remove a sample from the draft |
| POST | `/orgs/{org}/documents/{id}/text-in-region` | text a drawn box encloses (fills the value) |

`text-in-region` is the inverse of the review-time `locate` (box → text instead
of value → box): it reads the document's `text_geometry` artifact and returns
the real positioned text under the drawn box, so the annotation UI can pre-fill
the value. It never invents text — an empty box returns `""` and the labeller
types the value by hand.

Web screens: **Training** (`…/streams/$slug/training` — list + create),
**TrainingSet** (samples, publish, add-sample picker, train & measure) and
**TrainingAnnotate** (`…/training/$ts/documents/$docId` — the draw+label view).

## Phase 2 — train (few-shot)

Few-shot lives in the **versioned `examples` slot on the stream's instruction
content** — the closed instruction shape is now
`{instructions, field_guidance, examples}` (`instructions.py`), bounded to
`MAX_EXAMPLES` exemplars of `MAX_EXAMPLE_TEXT_CHARS` each. Because examples ride
inside `instruction_versions`, they are versioned, pinned per run, and
attributable exactly like instructions — no new runtime-pin plumbing.

`POST …/training-sets/{slug}/compile-examples` reads the published set's
`train`-split samples, pairs each sample's **real document-text excerpt** with
its expected `fields`/`lines` (an input→output pair, so the model learns the
mapping instead of parroting values), and writes them into an instruction draft
on the stream's active version (preserving any existing prompt text/guidance).
Publishing that instruction draft (the normal instructions publish flow) makes
the few-shot live. Because it writes sensitive prompt config, compile requires
**`instructions.manage`** (plus `streams.manage`), the same separation of duties
the instructions router enforces.

The request builder (`model_request_builder.py`) injects the exemplars as a
`worked_examples` array in the **user** message — the data side of the
injection-safe boundary — bounded and control-stripped, with a fixed platform
prompt line telling the model they are reference-only. **URLs anywhere in
instruction content are refused** (an exfiltration channel), so example text and
values are URL-scrubbed at compile time.

## Phase 3 — measure + gate

A published training set **is** the immutable gold dataset the evaluation runner
scores against, but it scores **only the held-out (`validation`/`test`) split** —
never `train`, whose documents were compiled verbatim into the live few-shot
examples (scoring memorised documents would inflate the very metrics the gate
reads). This is enforced by `evaluation_runs.scored_splits` (migration `0055`):
the training evaluation sets it to the held-out splits, and the worker plus the
server-mode preconditions score exactly that filtered set. **A training set
therefore needs both a `train` split (for few-shot) and a `validation`/`test`
split (for evaluation)** — evaluating a train-only set is refused.

- `POST …/training-sets/{slug}/evaluate` scores a stream config (the active
  version by default) against the set's held-out slice. Run it once for a
  baseline, then on the trained candidate with `baseline_run_id` — the report
  carries per-field accuracy (`by_field`, `by_cohort`) and the gate carries the
  before/after `field_diffs`.
- `GET …/training-sets/{slug}/evaluations` lists those runs.

The **regression gate is the existing promotion gate** (`evaluation_gate.py`):
critical-field regressions and rising false-auto-approval are non-waivable,
plus absolute accuracy floors. To make a trained config live you publish a new
stream version, which is gated on a passing server-executed evaluation — so a
training set that regresses critical fields is blocked from going live.

Server evaluations re-run extraction on the samples' source documents with the
candidate's pinned config, so they need every gold document to reference a
tenant source document whose original artifact still matches its hash (samples
uploaded through the normal pipeline satisfy this).

## End-to-end (local dev)

1. Upload sample PDFs to a stream and let them process to `review_required`.
2. Create a training set, open each sample in the annotation view, draw+label,
   save, then **publish** the set.
3. **Compile few-shot examples & publish** (UI button, or the compile +
   instructions-publish endpoints). Restart the worker if you changed worker
   code — it does not auto-reload.
4. Upload a new document — its run now pins the examples instruction and the
   request carries `worked_examples`.
5. **Evaluate on the held-out slice** to see per-field accuracy; add a baseline
   to see before/after and let the gate block regressions.

### Gotchas

- The worker does **not** auto-reload; restart it after changing
  `model_request_builder.py` or other worker code.
- Few-shot is pinned at intake, so only uploads **after** publishing the
  examples instruction carry it.
- Compiling needs the stream to have an active published version (for the
  schema) and the training set to be published (so the exemplars are
  reproducible).
- Server-mode evaluation with the local LLM re-extracts each sample and is slow
  (minutes per document); keep held-out slices small for quick iteration.
