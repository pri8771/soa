# Document routing (classifiers, buckets, skills)

A **skill** is a stream: the trained, versioned extraction capability a
document is processed by. An **intake (bucket)** is a stream documents arrive
at. A stream with a **published classifier** acts as a router: arriving
documents are matched against the routing table on their own text and handed
to the winning skill before extraction. A single-skill bucket is just an
intake with no classifier — same pipeline, no special case.

```
intake (bucket) ──0..1── classifier ──routes──▶ skills (streams)
UK & Ireland             uk-router v4          Pharma / Retail / NHS
Iberia                   (none — direct)       Iberia Distribution
```

This is deliberately **not** a parent-child hierarchy: skills stay flat and
reusable (several intakes can feed one skill), and a route's target can later
be another intake for multi-level classification without schema changes.

## The routing table

`classifier_versions` (migration `0056`) mirrors the instruction-version
discipline — per-intake numbering, draft → published → superseded, one
published version, immutable once published, forced RLS. The content is a
closed shape:

```json
{"routes": [{"label": "pharma-wholesale",
             "target_stream_id": "<stream uuid>",
             "signals": ["mawdsley", "phoenix healthcare"]}]}
```

**v1 matching is deterministic and auditable**: case-insensitive substring
match of each route's signals (customer names, letterheads) against the native
text of the document's first two pages; the route with the most distinct hits
wins; **ties or zero hits never guess** — the document goes to a human. An
LLM-based classifier can replace the matcher later behind the same routing
table without touching the lifecycle.

## Runtime

The worker's `classifying` stage (previously a pass-through):

1. No published classifier on the document's stream → unchanged.
2. Match with a different target → the routing run closes, the document
   re-queues under the **target** stream (`classifying → queued`), an audit
   event `document.routed` records the exact classifier version + matched
   signals, and a fresh preprocess starts a new run against the target's own
   immutable pins — resolved worker-side by `resolve_stream_pins`
   (fail-closed on anything missing or mutable).
3. No match / tie / no native text (scanned docs) → the document fails closed
   as **unrouted** (`failed_terminal`, reason contains `unrouted:`) for a
   human decision.

**A manual routing decision is authoritative**: runs started by the route
endpoint carry `triggered_by=manual-route` and the classify stage honors them
without re-classifying — otherwise routing an unroutable document into an
intake with a classifier would bounce it straight back to unrouted.

## API

| Method | Path | Permission |
|---|---|---|
| GET | `/orgs/{org}/streams/{slug}/classifier` | streams.read |
| POST | `/orgs/{org}/streams/{slug}/classifier` | streams.manage (targets must exist, not archived) |
| PATCH | `/orgs/{org}/classifier-versions/{id}` | streams.manage (draft only) |
| POST | `/orgs/{org}/classifier-versions/{id}/publish` | streams.manage |
| GET | `/orgs/{org}/routing/unrouted` | documents.read |
| POST | `/orgs/{org}/documents/{id}/route` | documents.reprocess |

Manual routes re-pin against the target's currently published configuration
and audit `document.routed` with the acting user — each decision is a labelled
example a future classifier version can train on.

## UI

The Skills home groups skills by process (the bucket grouping until intakes
are first-class) and shows a safety-critical **Unrouted band** whenever the
classifier refused to guess: each document routes inline to a chosen skill.
A skill's dashboard opens with its operating metric rail (queue, 30-day
volume, measured accuracy, training state).

## Gotchas

- The worker does **not** auto-reload — restart it after touching the classify
  stage or `resolve_stream_pins`.
- Classification reads **native text only** (no OCR): scanned documents always
  land unrouted until OCR is wired. That is fail-closed by design.
- Routing happens per document at the classify stage, so documents uploaded
  before a classifier is published are not retroactively routed (reprocess
  them to route).
- The worker is single-claim: a long LLM extraction delays classification of
  queued documents behind it (known concurrency gap).
