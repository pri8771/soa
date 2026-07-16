/** Deterministic API mocks (MSW) for web tests. */

import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";

import { DEFAULT_ME } from "./me-payload";

export { DEFAULT_ME };

export const DEFAULT_JOBS = {
  items: [
    {
      id: "11111111-1111-4111-8111-111111111111",
      job_type: "document.extract",
      status: "pending",
      priority: 100,
      attempts: 0,
      max_attempts: 5,
      run_after: "2026-07-12T10:00:00+00:00",
      created_at: "2026-07-12T10:00:00+00:00",
      finished_at: null,
      last_error: null,
      correlation_id: "corr-1",
    },
    {
      id: "22222222-2222-4222-8222-222222222222",
      job_type: "export.webhook",
      status: "dead_letter",
      priority: 100,
      attempts: 5,
      max_attempts: 5,
      run_after: "2026-07-12T09:00:00+00:00",
      created_at: "2026-07-12T09:00:00+00:00",
      finished_at: "2026-07-12T09:40:00+00:00",
      last_error: "delivery endpoint returned 503",
      correlation_id: "corr-2",
    },
  ],
  has_more: false,
  next_cursor: null,
};

export const DEFAULT_JOB_STATS = {
  by_status: { pending: 4, running: 1, succeeded: 20, dead_letter: 2, cancelled: 0 },
  oldest_pending_run_after: "2026-07-12T08:00:00+00:00",
};

export const DEFAULT_PROCESSES = [
  {
    id: "31111111-1111-4111-8111-111111111111",
    name: "Purchase orders",
    slug: "purchase-orders",
    status: "active",
    active_version_id: "32222222-2222-4222-8222-222222222222",
    active_version_number: 3,
    streams_count: 2,
    draft_count: 1,
    version: 4,
  },
  {
    id: "33333333-3333-4333-8333-333333333333",
    name: "Order confirmations",
    slug: "order-confirmations",
    status: "archived",
    active_version_id: null,
    active_version_number: null,
    streams_count: 0,
    draft_count: 0,
    version: 1,
  },
];

export const DEFAULT_STREAMS = [
  {
    id: "41111111-1111-4111-8111-111111111111",
    process_id: "31111111-1111-4111-8111-111111111111",
    name: "Email intake",
    slug: "email",
    status: "active",
    active_version_id: "42222222-2222-4222-8222-222222222222",
    process_name: "Purchase orders",
    process_slug: "purchase-orders",
    active_version_number: 2,
  },
];

export const DEFAULT_STREAM_DRAFT = {
  id: "43333333-3333-4333-8333-333333333333",
  version_number: 3,
  state: "draft",
  overrides: { confidence_floor: 0.9 },
  resolved_snapshot: null,
  pinned_process_version_id: null,
  version: 1,
};

export const DEFAULT_STREAM_DETAIL = {
  stream: {
    id: "41111111-1111-4111-8111-111111111111",
    process_id: "31111111-1111-4111-8111-111111111111",
    name: "Email intake",
    slug: "email",
    status: "active",
    active_version_id: "42222222-2222-4222-8222-222222222222",
  },
  versions: [
    {
      id: "40000000-0000-4000-8000-000000000001",
      version_number: 1,
      state: "superseded",
      overrides: {},
      resolved_snapshot: {
        process_version_id: "32222222-2222-4222-8222-222222222222",
        process_version_number: 3,
        config: { language: "en" },
      },
      pinned_process_version_id: "32222222-2222-4222-8222-222222222222",
      version: 2,
    },
    {
      id: "42222222-2222-4222-8222-222222222222",
      version_number: 2,
      state: "published",
      overrides: { confidence_floor: 0.95 },
      resolved_snapshot: {
        process_version_id: "32222222-2222-4222-8222-222222222222",
        process_version_number: 3,
        config: { language: "en", confidence_floor: 0.95 },
      },
      pinned_process_version_id: "32222222-2222-4222-8222-222222222222",
      version: 3,
    },
    DEFAULT_STREAM_DRAFT,
  ],
};

export const RESOLVE_ENVIRONMENT = { language: "en", confidence_floor: 0.85, max_pages: 50 };
export const RESOLVE_PROCESS = {
  language: "de",
  schema_version_id: "50000000-0000-4000-8000-000000000001",
};

/** Mirrors the server's resolver shape: layers + provenance-tagged values,
 * recomputed from the overrides the client actually sent. */
export function buildResolvePreview(overrides: Record<string, unknown>) {
  const values: Record<string, { value: unknown; source: string }> = {};
  for (const [key, value] of Object.entries(RESOLVE_ENVIRONMENT)) {
    values[key] = { value, source: "environment" };
  }
  for (const [key, value] of Object.entries(RESOLVE_PROCESS)) {
    values[key] = { value, source: "process" };
  }
  for (const [key, value] of Object.entries(overrides)) {
    values[key] = { value, source: "stream" };
  }
  return {
    layers: { environment: RESOLVE_ENVIRONMENT, process: RESOLVE_PROCESS, stream: overrides },
    resolved: {
      process_version_id: "32222222-2222-4222-8222-222222222222",
      process_version_number: 3,
      policies: [],
      values,
      fingerprint: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    },
  };
}

export const DEFAULT_SCHEMA_DRAFT = {
  id: "51111111-1111-4111-8111-111111111111",
  version_number: 2,
  state: "draft",
  definition: {
    fields: [
      {
        key: "po_number",
        label: "PO number",
        type: "text",
        required: true,
        criticality: "critical",
      },
      { key: "total", label: "Total", type: "money", criticality: "standard" },
    ],
  },
  change_summary: null,
  version: 1,
};

export const DEFAULT_SCHEMA_LISTING = {
  versions: [
    {
      id: "50000000-0000-4000-8000-000000000001",
      version_number: 1,
      state: "published",
      definition: { fields: [{ key: "po_number", label: "PO number", type: "text" }] },
      change_summary: null,
      version: 2,
    },
    DEFAULT_SCHEMA_DRAFT,
  ],
  published_json_schema: { type: "object" },
};

export const PROCESS_VERSION_IDS = {
  v1: "30000000-0000-4000-8000-000000000001",
  v2: "30000000-0000-4000-8000-000000000002",
  v3: "32222222-2222-4222-8222-222222222222",
};

export const DEFAULT_PROCESS_DETAIL = {
  process: {
    id: "31111111-1111-4111-8111-111111111111",
    name: "Purchase orders",
    slug: "purchase-orders",
    status: "active",
    active_version_id: PROCESS_VERSION_IDS.v3,
    version: 4,
  },
  versions: [
    {
      id: PROCESS_VERSION_IDS.v1,
      version_number: 1,
      state: "superseded",
      change_summary: "initial configuration",
      published_at: "2026-07-01T10:00:00+00:00",
      published_by: "user:u-1",
      version: 3,
    },
    {
      id: PROCESS_VERSION_IDS.v2,
      version_number: 2,
      state: "superseded",
      change_summary: "raise confidence floor",
      published_at: "2026-07-05T10:00:00+00:00",
      published_by: "user:u-1",
      version: 3,
    },
    {
      id: PROCESS_VERSION_IDS.v3,
      version_number: 3,
      state: "published",
      change_summary: "switch to German",
      published_at: "2026-07-10T10:00:00+00:00",
      published_by: "user:u-1",
      version: 2,
    },
  ],
};

/** v2 embeds a secret-looking key so tests can prove the diff redacts it. */
export const PROCESS_VERSION_DEFINITIONS: Record<string, Record<string, unknown>> = {
  [PROCESS_VERSION_IDS.v1]: { language: "en" },
  [PROCESS_VERSION_IDS.v2]: {
    language: "en",
    confidence_floor: 0.9,
    api_token: "sk-live-verysecret",
  },
  [PROCESS_VERSION_IDS.v3]: { language: "de", confidence_floor: 0.9 },
};

export const DEFAULT_RULES_DRAFT = {
  id: "61111111-1111-4111-8111-111111111111",
  version_number: 1,
  state: "draft",
  definition: {
    rules: [
      {
        key: "high-value",
        severity: "warning",
        action: "route_to_review",
        condition: {
          op: "gt",
          left: { op: "field", key: "total" },
          right: { op: "const", value: 5000 },
        },
        test_cases: [{ values: { total: 9000 }, expect_triggered: true }],
      },
    ],
  },
  change_summary: null,
  version: 1,
};

export const DEFAULT_RULES_LISTING = {
  versions: [DEFAULT_RULES_DRAFT],
  field_types: { po_number: "text", total: "money" },
};

export const DEFAULT_DOCUMENTS = [
  {
    id: "81111111-1111-4111-8111-111111111111",
    stream_id: "41111111-1111-4111-8111-111111111111",
    state: "queued",
    state_reason: null,
    source_channel: "upload",
    original_filename: "po-4711.pdf",
    content_sha256: "a".repeat(64),
    size_bytes: 204800,
    content_type: "application/pdf",
    client_reference: "batch-1",
    priority: 100,
    sla_due_at: null,
    received_at: "2026-07-12T09:00:00+00:00",
    duplicate_of: null,
  },
  {
    id: "82222222-2222-4222-8222-222222222222",
    stream_id: "41111111-1111-4111-8111-111111111111",
    state: "queued",
    state_reason: null,
    source_channel: "api",
    original_filename: "po-4712.pdf",
    content_sha256: "b".repeat(64),
    size_bytes: 105000,
    content_type: "application/pdf",
    client_reference: null,
    priority: 100,
    sla_due_at: null,
    received_at: "2026-07-12T09:05:00+00:00",
    duplicate_of: "81111111-1111-4111-8111-111111111111",
  },
  {
    id: "83333333-3333-4333-8333-333333333333",
    stream_id: "41111111-1111-4111-8111-111111111111",
    state: "quarantined",
    state_reason: "malware detected: Win.Test.EICAR_HDB-1",
    source_channel: "upload",
    original_filename: "invoice-evil.pdf",
    content_sha256: "c".repeat(64),
    size_bytes: 1024,
    content_type: "application/pdf",
    client_reference: null,
    priority: 100,
    sla_due_at: null,
    received_at: "2026-07-12T09:10:00+00:00",
    duplicate_of: null,
  },
  {
    id: "84444444-4444-4444-8444-444444444444",
    stream_id: "41111111-1111-4111-8111-111111111111",
    state: "failed_retryable",
    state_reason: "stage extracting failed: provider timeout",
    source_channel: "upload",
    original_filename: "po-4713.pdf",
    content_sha256: "d".repeat(64),
    size_bytes: 88000,
    content_type: "application/pdf",
    client_reference: null,
    priority: 100,
    sla_due_at: null,
    received_at: "2026-07-12T09:20:00+00:00",
    duplicate_of: null,
  },
  {
    id: "85555555-5555-4555-8555-555555555555",
    stream_id: "",
    state: "deleted",
    state_reason: "document data deleted",
    source_channel: "",
    original_filename: "[deleted]",
    content_sha256: "",
    size_bytes: 0,
    content_type: "application/octet-stream",
    client_reference: null,
    priority: 0,
    sla_due_at: null,
    received_at: "2026-07-15T12:05:00+00:00",
    duplicate_of: null,
  },
];

export const DEFAULT_DELETION_REQUEST = {
  id: "d9999999-9999-4999-8999-999999999999",
  document_id: DEFAULT_DOCUMENTS[2].id,
  state: "pending_approval",
  reason: "customer erasure request",
  requested_by: "u-1",
  requested_at: "2026-07-15T12:00:00Z",
  approved_by: null,
  approval_reason: null,
  approved_at: null,
  completed_at: null,
  cancelled_by: null,
  cancellation_reason: null,
  cancelled_at: null,
  safe_error: null,
  version: 1,
} as const;

export const DEFAULT_LEGAL_HOLD = {
  id: "e9999999-9999-4999-8999-999999999999",
  document_id: DEFAULT_DOCUMENTS[2].id,
  state: "active",
  reason: "litigation preservation order",
  placed_by: "u-3",
  placed_at: "2026-07-15T11:00:00Z",
  released_by: null,
  release_reason: null,
  released_at: null,
  version: 1,
} as const;

export const DEFAULT_RUNS = [
  {
    id: "a1111111-1111-4111-8111-111111111111",
    run_number: 1,
    state: "failed",
    triggered_by: "system:orchestrator",
    stream_version_id: "51111111-1111-4111-8111-111111111111",
    config_fingerprint: "f".repeat(64),
    contract_fingerprint: "c".repeat(64),
    runtime_fingerprint: "r".repeat(64),
    runtime_provenance: {
      renderer: { name: "pdfium", version: "pinned" },
      model: { provider: "mock", endpoint_digest: "sha256:runtime" },
    },
    started_at: "2026-07-12T09:21:00+00:00",
    finished_at: "2026-07-12T09:24:00+00:00",
    total_latency_ms: 5340,
    total_cost_cents: 4,
    stages: [
      {
        stage: "preprocessing",
        attempt: 1,
        state: "succeeded",
        provider: null,
        latency_ms: 1200,
        cost_cents: 0,
        safe_error: null,
        failure_class: null,
        output_summary: { pages: 2 },
        started_at: "2026-07-12T09:21:00+00:00",
        finished_at: "2026-07-12T09:21:02+00:00",
      },
      {
        stage: "extracting",
        attempt: 1,
        state: "failed",
        provider: "mock",
        latency_ms: 900,
        cost_cents: 2,
        safe_error: "mock provider configured to fail (retryable)",
        failure_class: "retryable",
        output_summary: {},
        started_at: "2026-07-12T09:22:00+00:00",
        finished_at: "2026-07-12T09:22:01+00:00",
      },
      {
        stage: "extracting",
        attempt: 2,
        state: "failed",
        provider: "mock",
        latency_ms: 880,
        cost_cents: 2,
        safe_error: "mock provider configured to fail (retryable)",
        failure_class: "retryable",
        output_summary: { warnings: ["provider responded slowly before failing"] },
        started_at: "2026-07-12T09:23:00+00:00",
        finished_at: "2026-07-12T09:23:01+00:00",
      },
    ],
  },
];

export const DEFAULT_REVIEW_TASKS = [
  {
    id: "b1111111-1111-4111-8111-111111111111",
    document_id: "84444444-4444-4444-8444-444444444444",
    run_id: "a1111111-1111-4111-8111-111111111111",
    state: "open",
    priority: 10,
    blocking: true,
    sla_due_at: "2026-07-12T08:00:00+00:00", // in the past: overdue
    assigned_to: null,
    assigned_at: null,
    reasons: [
      {
        code: "rule_triggered",
        message: "Order total disagrees with the sum of line totals",
        field_key: null,
        row_index: null,
        rule_key: "totals.header_matches_lines",
      },
      {
        code: "low_confidence",
        message: "confidence 0.60 is below the critical gate of 0.98",
        field_key: "po_number",
        row_index: null,
        rule_key: null,
      },
    ],
    outcome: null,
    version: 1,
    created_at: "2026-07-12T09:25:00+00:00",
    document_filename: "po-4713.pdf",
    document_state: "review_required",
  },
  {
    id: "b2222222-2222-4222-8222-222222222222",
    document_id: "81111111-1111-4111-8111-111111111111",
    run_id: "a2222222-2222-4222-8222-222222222222",
    state: "in_progress",
    priority: 100,
    blocking: false,
    sla_due_at: null,
    assigned_to: "user:u-1",
    assigned_at: "2026-07-12T10:00:00+00:00",
    reasons: [
      {
        code: "ambiguous_reading",
        message: "a rival reading is within the margin",
        field_key: "currency",
        row_index: null,
        rule_key: null,
      },
    ],
    outcome: null,
    version: 2,
    created_at: "2026-07-12T09:30:00+00:00",
    document_filename: "po-4711.pdf",
    document_state: "review_required",
  },
];

export const DEFAULT_WORKSPACE = {
  task: {
    ...DEFAULT_REVIEW_TASKS[1],
    id: "b2222222-2222-4222-8222-222222222222",
    state: "in_progress",
    assigned_to: "user:u-1",
    version: 3,
    reasons: [
      {
        code: "low_confidence",
        message: "confidence 0.60 is below the critical gate of 0.98",
        field_key: "po_number",
        row_index: null,
        rule_key: null,
      },
    ],
  },
  superseded_by_task_id: null,
  document: {
    id: "84444444-4444-4444-8444-444444444444",
    state: "review_required",
    state_reason: null,
    original_filename: "po-4713.pdf",
    priority: 10,
    received_at: "2026-07-12T09:20:00+00:00",
  },
  run: {
    id: "a1111111-1111-4111-8111-111111111111",
    run_number: 1,
    state: "succeeded",
    decision: {
      route: "review_required",
      reasons: [{ code: "low_confidence", field_key: "po_number" }],
    },
  },
  fields: [
    {
      field_key: "po_number",
      row_index: null,
      raw_value: "PO-1000A2",
      normalized_value: "PO-1000A2",
      normalization_error: null,
      confidence: 0.6,
      validation_status: "review",
      provider: "mock",
      provider_model: "mock-v1",
      evidence: [
        {
          page_number: 1,
          certainty: "region",
          polygon: [
            [170, 220],
            [340, 220],
            [340, 440],
            [170, 440],
          ],
          quote: "PO-1000A2",
        },
      ],
      candidates: [{ raw_value: "PO-100042", confidence: 0.55 }],
    },
    {
      field_key: "total_amount",
      row_index: null,
      raw_value: "1,234.50",
      normalized_value: { amount: "1234.50", currency: "USD" },
      normalization_error: null,
      confidence: 0.96,
      validation_status: "passed",
      provider: "mock",
      provider_model: "mock-v1",
      evidence: [{ page_number: 2, certainty: "page", polygon: null, quote: null }],
      candidates: [],
    },
  ],
  line_items: {
    lines: [
      [
        {
          field_key: "lines.sku",
          row_index: 0,
          raw_value: "WID-100",
          normalized_value: "WID-100",
          normalization_error: null,
          confidence: 0.95,
          validation_status: "passed",
          provider: "mock",
          provider_model: "mock-v1",
          evidence: [
            {
              page_number: 1,
              certainty: "region",
              polygon: [
                [100, 900],
                [300, 900],
                [300, 960],
                [100, 960],
              ],
              quote: "WID-100",
            },
          ],
          candidates: [],
        },
        {
          field_key: "lines.quantity",
          row_index: 0,
          raw_value: "10",
          normalized_value: "10",
          normalization_error: null,
          confidence: 0.95,
          validation_status: "passed",
          provider: "mock",
          provider_model: "mock-v1",
          evidence: [],
          candidates: [],
        },
        {
          field_key: "lines.unit_price",
          row_index: 0,
          raw_value: "45.00",
          normalized_value: { amount: "45.00", currency: "USD" },
          normalization_error: null,
          confidence: 0.95,
          validation_status: "passed",
          provider: "mock",
          provider_model: "mock-v1",
          evidence: [],
          candidates: [],
        },
        {
          field_key: "lines.line_total",
          row_index: 0,
          raw_value: "450.00",
          normalized_value: { amount: "450.00", currency: "USD" },
          normalization_error: null,
          confidence: 0.95,
          validation_status: "passed",
          provider: "mock",
          provider_model: "mock-v1",
          evidence: [],
          candidates: [],
        },
      ],
      [
        {
          field_key: "lines.sku",
          row_index: 1,
          raw_value: "GAD-205",
          normalized_value: "GAD-205",
          normalization_error: null,
          confidence: 0.88,
          validation_status: "passed",
          provider: "mock",
          provider_model: "mock-v1",
          evidence: [],
          candidates: [],
        },
        {
          field_key: "lines.quantity",
          row_index: 1,
          raw_value: "3",
          normalized_value: "3",
          normalization_error: null,
          confidence: 0.88,
          validation_status: "passed",
          provider: "mock",
          provider_model: "mock-v1",
          evidence: [],
          candidates: [],
        },
        {
          field_key: "lines.line_total",
          row_index: 1,
          raw_value: "784.50",
          normalized_value: { amount: "784.50", currency: "USD" },
          normalization_error: null,
          confidence: 0.88,
          validation_status: "passed",
          provider: "mock",
          provider_model: "mock-v1",
          evidence: [],
          candidates: [],
        },
      ],
    ],
  },
  corrections: [],
  pages: [
    {
      page_number: 1,
      width_px: 1700,
      height_px: 2200,
      dpi: 200,
      rotation_degrees: 0,
      content_type: "image/png",
      image_artifact_id: "c1111111-1111-4111-8111-111111111111",
      text_artifact_id: null,
    },
    {
      page_number: 2,
      width_px: 1700,
      height_px: 2200,
      dpi: 200,
      rotation_degrees: 0,
      content_type: "image/png",
      image_artifact_id: "c2222222-2222-4222-8222-222222222222",
      text_artifact_id: null,
    },
  ],
  history: [],
  context: { stream_slug: "email", config_fingerprint: "f".repeat(64) },
};

export const DEFAULT_INTEGRATION = {
  id: "e1111111-1111-4111-8111-111111111111",
  name: "Northstar ERP webhook",
  slug: "erp",
  integration_type: "webhook",
  status: "active",
  endpoint_url: "https://erp.northstar.example/orders",
  credential_configured: true,
  production_ready: true,
  readiness_detail: "Signed webhook delivery is production-ready.",
  idempotency_mechanism: "X-SOA-Idempotency-Key header (business key)",
  active_mapping_version_id: null,
  version: 2,
  created_at: "2026-07-12T09:00:00+00:00",
};

export const DEFAULT_MAPPING_DRAFT = {
  id: "e2222222-2222-4222-8222-222222222222",
  integration_id: DEFAULT_INTEGRATION.id,
  version_number: 1,
  state: "draft",
  definition: {
    fields: [
      { target: "PoNumber", source: "identifiers.po_number", required: true },
      {
        target: "OrderDate",
        source: "dates.order_date",
        format: { kind: "date", pattern: "MM/DD/YYYY" },
      },
    ],
    constants: [{ target: "SourceSystem", value: "SOA" }],
    lines: {
      source: "line_items",
      target: "Lines",
      fields: [{ target: "Sku", source: "sku" }],
    },
  },
  target_schema: { type: "object", required: ["PoNumber", "GrandTotal"] },
  change_summary: null,
  published_at: null,
  published_by: null,
  version: 3,
};

export const handlers = [
  // No fixture document has export jobs by default; delivery tests seed
  // their own via server.use.
  http.get("/api/orgs/:slug/exports", () => HttpResponse.json({ items: [] })),
  http.get("/api/orgs/:slug/integrations", () =>
    HttpResponse.json({ items: [DEFAULT_INTEGRATION] }),
  ),
  http.post("/api/orgs/:slug/integrations", async ({ request }) => {
    const body = (await request.json()) as {
      name: string;
      slug: string;
      integration_type: string;
      endpoint_url: string | null;
    };
    return HttpResponse.json(
      {
        id: "integration-created",
        ...body,
        status: "draft",
        credential_configured: false,
        production_ready:
          body.integration_type === "webhook" || body.integration_type === "quickbooks_online",
        readiness_detail:
          body.integration_type === "webhook" || body.integration_type === "quickbooks_online"
            ? "Delivery is production-ready."
            : "A vendor-specific idempotent upsert contract is still required.",
        idempotency_mechanism:
          body.integration_type === "webhook" || body.integration_type === "quickbooks_online"
            ? "stable business idempotency key"
            : "Not implemented; delivery is disabled.",
        active_mapping_version_id: null,
        version: 1,
        created_at: "2026-07-15T12:00:00Z",
      },
      { status: 201 },
    );
  }),
  http.put("/api/orgs/:slug/integrations/:integrationSlug/credential", async ({ request }) => {
    const body = (await request.json()) as { kind: string; secret: string };
    return HttpResponse.json({ credential_configured: true, kind: body.kind, rotated: false });
  }),
  http.post("/api/orgs/:slug/integrations/:integrationSlug/connection-test", () =>
    HttpResponse.json({ ok: true, detail: "receiver accepted a signed test event" }),
  ),
  http.post("/api/orgs/:slug/integrations/:integrationSlug/activate", () =>
    HttpResponse.json({
      activated: true,
      detail: "Connection verified; integration activated.",
      integration: { ...DEFAULT_INTEGRATION, status: "active", version: 3 },
    }),
  ),
  http.post("/api/orgs/:slug/integrations/:integrationSlug/deactivate", () =>
    HttpResponse.json({ ...DEFAULT_INTEGRATION, status: "paused", version: 3 }),
  ),
  http.post("/api/orgs/:slug/integrations/:integrationSlug/archive", () =>
    HttpResponse.json({ ...DEFAULT_INTEGRATION, status: "archived", version: 3 }),
  ),
  http.get("/api/orgs/:slug/integrations/:integrationSlug", () =>
    HttpResponse.json({
      integration: DEFAULT_INTEGRATION,
      mapping_versions: [DEFAULT_MAPPING_DRAFT],
    }),
  ),
  http.patch("/api/orgs/:slug/integrations/:integrationSlug/mapping-versions/:versionId", () =>
    HttpResponse.json({ ...DEFAULT_MAPPING_DRAFT, version: 4 }),
  ),
  http.post(
    "/api/orgs/:slug/integrations/:integrationSlug/mapping-versions/:versionId/validate",
    () =>
      HttpResponse.json({
        valid: true,
        errors: [],
        payload: { PoNumber: "PO-100042", OrderDate: "03/14/2026", SourceSystem: "SOA" },
        trace: [],
        notes: [],
      }),
  ),
  http.post(
    "/api/orgs/:slug/integrations/:integrationSlug/mapping-versions/:versionId/publish",
    () => HttpResponse.json({ ...DEFAULT_MAPPING_DRAFT, state: "published", version: 4 }),
  ),
  http.post("/api/orgs/:slug/integrations/:integrationSlug/mapping-versions", () =>
    HttpResponse.json({ ...DEFAULT_MAPPING_DRAFT, id: "e3", version_number: 2 }, { status: 201 }),
  ),
  http.get("/api/orgs/:slug/review-tasks/:taskId/workspace", () =>
    HttpResponse.json(DEFAULT_WORKSPACE),
  ),
  http.get("/api/orgs/:slug/review-tasks/:taskId/comments", () => HttpResponse.json({ items: [] })),
  http.post("/api/orgs/:slug/review-tasks/:taskId/comments", async ({ request, params }) => {
    const body = (await request.json()) as { body: string };
    return HttpResponse.json(
      {
        id: "c1111111-1111-4111-8111-111111111111",
        task_id: String(params["taskId"]),
        document_id: DEFAULT_WORKSPACE.document.id,
        author: "user:u-1",
        body: body.body,
        mentions: [],
        created_at: "2026-07-12T10:05:00+00:00",
      },
      { status: 201 },
    );
  }),
  http.post("/api/orgs/:slug/review-tasks/:taskId/corrections", async ({ request }) => {
    const body = (await request.json()) as { field_key: string; value: string | null };
    return HttpResponse.json({
      correction: {
        id: "cor-1",
        field_key: body.field_key,
        row_index: null,
        previous_raw_value: "PO-1000A2",
        corrected_raw_value: body.value,
        corrected_normalized_value: body.value,
        normalization_error: null,
        corrected_by: "user:u-1",
      },
      task_version: 4,
      revalidation: {
        evaluation: { blocking: false },
        decision: { route: "approved", reasons: [] },
      },
    });
  }),
  http.get("/api/orgs/:slug/audit-events", ({ request }) => {
    const url = new URL(request.url);
    const action = url.searchParams.get("action") ?? "";
    const items = [
      {
        id: "f1111111-1111-4111-8111-111111111111",
        occurred_at: "2026-07-12T10:00:00+00:00",
        actor_type: "user",
        actor_id: "user:u-1",
        action: "document.approved",
        target_type: "document",
        target_id: "d-1",
        summary: {},
      },
      {
        id: "f2222222-2222-4222-8222-222222222222",
        occurred_at: "2026-07-12T09:00:00+00:00",
        actor_type: "system",
        actor_id: "system:worker",
        action: "catalog.match_selected",
        target_type: "review_task",
        target_id: "t-1",
        summary: {},
      },
    ].filter((item) => item.action.startsWith(action));
    return HttpResponse.json({ items, has_more: false, next_cursor: null });
  }),
  http.post("/api/orgs/:slug/audit-exports", () =>
    HttpResponse.json(
      {
        export_id: "e9999999-9999-4999-8999-999999999999",
        event_count: 2,
        manifest_sha256: "ab".repeat(32),
        manifest_download_url: "memory://store/audit-exports/manifest.json?sig=x",
        manifest_expires_at: "2026-07-13T13:00:00+00:00",
        files: [
          {
            name: "events-0001.ndjson",
            sha256: "cd".repeat(32),
            bytes: 512,
            events: 2,
            download_url: "memory://store/audit-exports/events-0001.ndjson?sig=x",
            expires_at: "2026-07-13T13:00:00+00:00",
          },
        ],
      },
      { status: 201 },
    ),
  ),
  http.get("/api/orgs/:slug/analytics/usage", () =>
    HttpResponse.json({
      window: {
        since: "2026-06-29T00:00:00+00:00",
        until: "2026-07-13T00:00:00+00:00",
        timezone: "UTC",
      },
      groups: [
        {
          stream_id: null,
          provider: "hosted-ocr",
          provider_model: null,
          cost_category: "ocr",
          billed_unit: "pages",
          billed_quantity: 120,
          pages: 120,
          entries: 12,
          estimated_cents: 1200,
          adjustment_cents: -300,
          reconciled_cents: 900,
        },
        {
          stream_id: null,
          provider: "local-llm",
          provider_model: "qwen",
          cost_category: "extraction",
          billed_unit: "tokens",
          billed_quantity: 512000,
          pages: 0,
          entries: 12,
          estimated_cents: 0,
          adjustment_cents: 0,
          reconciled_cents: 0,
        },
      ],
      totals: { estimated_cents: 1200, adjustment_cents: -300, reconciled_cents: 900 },
      semantics: {
        estimated_cents: "our price estimate at recording time",
        billed_quantity: "provider-metered units (facts), summed per billed_unit only",
        reconciled_cents: "estimated + appended adjustments; rows are immutable",
      },
      quotas: {
        configured: false,
        reason:
          "no quota policy is configured yet (ANA-009) — usage is shown without budget " +
          "thresholds or alerts",
      },
    }),
  ),
  http.get("/api/orgs/:slug/analytics/quality", () =>
    HttpResponse.json({
      window: {
        since: "2026-06-29T00:00:00+00:00",
        until: "2026-07-13T00:00:00+00:00",
        timezone: "UTC",
      },
      reviewed: { tasks_completed: 12, runs: 12 },
      field_corrections: [
        { field_key: "po_number", present_runs: 12, corrected_runs: 3, correction_rate: 0.25 },
        { field_key: "total_amount", present_runs: 12, corrected_runs: 0, correction_rate: 0.0 },
      ],
      line_corrections: { cells_present: 48, cells_corrected: 6, correction_rate: 0.125 },
      stp: { settled_documents: 20, straight_through: 8, stp_rate: 0.4 },
      false_auto_approval: {
        available: false,
        reason:
          "auto-approved documents have no reviewer to catch errors, so production data " +
          "cannot measure this; run the AIO-016 evaluation against a gold dataset " +
          "(AIO-015) for a defensible number",
      },
      calibration: {
        cohorts: [
          {
            confidence_range: "[0.0, 0.5)",
            fields_reviewed: 0,
            corrected: 0,
            correction_rate: null,
          },
          {
            confidence_range: "[0.5, 0.8)",
            fields_reviewed: 4,
            corrected: 3,
            correction_rate: 0.75,
          },
          {
            confidence_range: "[0.95, 1.0]",
            fields_reviewed: 40,
            corrected: 1,
            correction_rate: 0.025,
          },
        ],
      },
      ground_truth: {
        gold_documents: 0,
        note:
          "all rates above are correction-based PROXIES; accuracy claims require " +
          "evaluation against gold data",
      },
      notes: [],
      definitions: {
        "quality.field_correction_rate": {
          key: "quality.field_correction_rate",
          description:
            "How often reviewers corrected a field — a PROXY for accuracy, not accuracy.",
          numerator: "reviewed runs in which the field was corrected at least once",
          denominator: "reviewed runs in which the field appeared (extracted or corrected)",
          timezone: "UTC (day buckets are UTC calendar days; window is [since, until))",
        },
      },
    }),
  ),
  http.get("/api/orgs/:slug/analytics/operations", () =>
    HttpResponse.json({
      window: {
        since: "2026-06-29T00:00:00+00:00",
        until: "2026-07-13T00:00:00+00:00",
        timezone: "UTC",
      },
      stream_id: null,
      volume: {
        per_day: [
          { day: "2026-07-11", state: "completed", count: 4 },
          { day: "2026-07-12", state: "review_required", count: 2 },
        ],
      },
      latency: {
        runs_measured: 6,
        sample_capped: false,
        avg_ms: 412.5,
        p50_ms: 390,
        p95_ms: 940,
      },
      backlog: {
        documents_by_state: { review_required: 2, extracting: 1 },
        review_tasks: { open: 2, in_progress: 1, blocking: 1 },
      },
      exceptions: {
        documents_by_state: {
          quarantined: 1,
          failed_retryable: 0,
          failed_terminal: 1,
          rejected: 0,
          cancelled: 0,
        },
      },
      sla: {
        overdue_now: 1,
        active_with_sla: 3,
        breached_completed: 1,
        completed_with_sla: 4,
      },
      exports: {
        jobs_by_state: { succeeded: 3, failed_retryable: 1 },
        attempts_total: 5,
        attempts_delivered: 3,
      },
      notes: [],
      definitions: {
        "sla.overdue_now": {
          key: "sla.overdue_now",
          description: "Active review tasks past their SLA right now (snapshot).",
          numerator: "open/in_progress tasks with sla_due_at < now",
          denominator: "active_with_sla: open/in_progress tasks that have an SLA",
          timezone: "UTC (day buckets are UTC calendar days; window is [since, until))",
        },
      },
      needs_attention: [
        {
          key: "overdue_reviews",
          label: "Review tasks past their SLA",
          count: 1,
          link: { screen: "review", filters: { view: "overdue" } },
        },
        {
          key: "quarantined_documents",
          label: "Quarantined documents",
          count: 1,
          link: { screen: "documents", filters: { docState: "quarantined" } },
        },
        {
          key: "failing_exports",
          label: "Export jobs failing in the window",
          count: 1,
          link: { screen: "integrations", filters: {} },
        },
      ],
    }),
  ),
  http.get("/api/orgs/:slug/review-tasks/:taskId/catalog-candidates", ({ request }) => {
    const url = new URL(request.url);
    const q = url.searchParams.get("q") ?? "";
    return HttpResponse.json({
      available: true,
      field_type: "material",
      outcome: "needs_review",
      machine_selected_source_id: null,
      reasons: [
        `top fuzzy score 0.82 is below the material auto-match threshold 0.92 — routed to review`,
      ],
      candidates: [
        {
          id: "d1111111-1111-4111-8111-111111111111",
          code: "WID-100",
          label: "Widget 100 (steel)",
          score: 0.82,
          features: [
            { name: "text_trigram", score: 0.8, explanation: `trigram overlap with 'Widget 100'` },
            {
              name: "text_sequence",
              score: 0.85,
              explanation: `sequence similarity with 'Widget 100'`,
            },
          ],
        },
        {
          id: "d2222222-2222-4222-8222-222222222222",
          code: "GAD-205",
          label: "Gadget 205",
          score: 0.55,
          features: [
            { name: "text_trigram", score: 0.5, explanation: `trigram overlap with 'Gadget 205'` },
            {
              name: "text_sequence",
              score: 0.61,
              explanation: `sequence similarity with 'Gadget 205'`,
            },
          ],
        },
      ].filter((candidate) => q !== "" || candidate.score > 0.6),
      task_version: 3,
    });
  }),
  http.post("/api/orgs/:slug/review-tasks/:taskId/catalog-selection", async ({ request }) => {
    const body = (await request.json()) as {
      field_key: string;
      row_index: number | null;
      selected_source_id: string | null;
      reason?: string;
    };
    return HttpResponse.json({
      correction:
        body.selected_source_id === null
          ? null
          : {
              id: "cor-cat-1",
              field_key: body.field_key,
              row_index: body.row_index,
              previous_raw_value: "WID-1OO",
              corrected_raw_value: body.selected_source_id,
              corrected_normalized_value: body.selected_source_id,
              normalization_error: null,
              corrected_by: "user:u-1",
            },
      task_version: 4,
      override: Boolean(body.reason),
      revalidation: {
        evaluation: { blocking: false },
        decision: { route: "approved", reasons: [] },
      },
    });
  }),
  http.get("/api/orgs/:slug/review-tasks", ({ request }) => {
    const url = new URL(request.url);
    const view = url.searchParams.get("view") ?? "all";
    let items = DEFAULT_REVIEW_TASKS;
    if (view === "mine") items = items.filter((t) => t.assigned_to === "user:u-1");
    if (view === "unassigned") items = items.filter((t) => t.state === "open");
    if (view === "overdue") items = items.filter((t) => t.sla_due_at !== null);
    if (view === "blocked") items = items.filter((t) => t.blocking);
    return HttpResponse.json({ items, has_more: false, next_cursor: null });
  }),
  http.post("/api/orgs/:slug/review-tasks/claim-next", () =>
    HttpResponse.json({
      task: { ...DEFAULT_REVIEW_TASKS[0], state: "in_progress", assigned_to: "user:u-1" },
      explanation: "Highest-priority open task, oldest first within the same priority.",
    }),
  ),
  // No approved fixture document carries a canonical payload by default;
  // tests seed one via server.use when they need it.
  http.get("/api/orgs/:slug/documents/:documentId/canonical-payload", () =>
    HttpResponse.json(
      {
        error: {
          message: "No canonical payload exists yet — it is created when the document is approved.",
        },
      },
      { status: 404 },
    ),
  ),
  http.post("/api/orgs/:slug/review-tasks/:taskId/approve", async ({ request }) => {
    const body = (await request.json()) as { override_reason?: string };
    return HttpResponse.json({
      status: "approved",
      idempotent: false,
      task_version: 5,
      warnings: [],
      override_used: Boolean(body.override_reason),
      task: { ...DEFAULT_WORKSPACE.task, state: "completed", outcome: "approved" },
    });
  }),
  http.post("/api/orgs/:slug/review-tasks/:taskId/reject", async ({ request }) => {
    const body = (await request.json()) as { reason?: string };
    if (!body.reason || body.reason.trim() === "") {
      return HttpResponse.json({ error: { message: "rejection needs a reason" } }, { status: 400 });
    }
    return HttpResponse.json({
      status: "rejected",
      idempotent: false,
      task_version: 5,
      task: { ...DEFAULT_WORKSPACE.task, state: "completed", outcome: "rejected" },
    });
  }),
  http.post("/api/orgs/:slug/review-tasks/:taskId/escalate", async ({ request }) => {
    const body = (await request.json()) as { reason?: string };
    if (!body.reason || body.reason.trim() === "") {
      return HttpResponse.json(
        { error: { message: "escalation needs a reason" } },
        { status: 400 },
      );
    }
    return HttpResponse.json({
      ...DEFAULT_WORKSPACE.task,
      state: "open",
      assigned_to: null,
      priority: 10,
      escalation_reason: body.reason,
    });
  }),
  http.post("/api/orgs/:slug/review-tasks/:taskId/claim", ({ params }) => {
    const found = DEFAULT_REVIEW_TASKS.find((t) => t.id === String(params["taskId"]));
    if (!found) {
      return HttpResponse.json({ error: { message: "Task not found." } }, { status: 404 });
    }
    return HttpResponse.json({ ...found, state: "in_progress", assigned_to: "user:u-1" });
  }),
  http.post("/api/orgs/:slug/review-tasks/:taskId/release", ({ params }) => {
    const found = DEFAULT_REVIEW_TASKS.find((t) => t.id === String(params["taskId"]));
    if (!found) {
      return HttpResponse.json({ error: { message: "Task not found." } }, { status: 404 });
    }
    return HttpResponse.json({ ...found, state: "open", assigned_to: null });
  }),
  http.get("/api/orgs/:slug/documents", ({ request }) => {
    const url = new URL(request.url);
    const state = url.searchParams.get("document_state");
    const channel = url.searchParams.get("source_channel");
    const search = url.searchParams.get("search");
    let items = DEFAULT_DOCUMENTS;
    // Deleted documents are a tombstone, not active work: an unfiltered
    // list hides them, mirroring the real API (routers/documents.py).
    if (state) items = items.filter((d) => d.state === state);
    else items = items.filter((d) => d.state !== "deleted");
    if (channel) items = items.filter((d) => d.source_channel === channel);
    if (search) items = items.filter((d) => d.original_filename.includes(search));
    return HttpResponse.json({ items, has_more: false, next_cursor: null });
  }),
  http.post("/api/orgs/:slug/documents/:documentId/cancel", ({ params }) =>
    HttpResponse.json({ id: String(params["documentId"]), state: "cancelled" }),
  ),
  http.post("/api/orgs/:slug/documents/:documentId/deletion-requests", ({ params }) =>
    HttpResponse.json(
      {
        ...DEFAULT_DELETION_REQUEST,
        document_id: String(params["documentId"]),
      },
      { status: 202 },
    ),
  ),
  http.get("/api/orgs/:slug/documents/:documentId/deletion-request", () =>
    HttpResponse.json({ error: { message: "Deletion request not found." } }, { status: 404 }),
  ),
  http.get("/api/orgs/:slug/deletion-requests", () => HttpResponse.json({ items: [] })),
  http.post("/api/orgs/:slug/deletion-requests/:requestId/approve", () =>
    HttpResponse.json(
      {
        ...DEFAULT_DELETION_REQUEST,
        state: "approved",
        approved_by: "u-2",
        approval_reason: "verified independent approval",
        approved_at: "2026-07-15T12:02:00Z",
        version: 2,
      },
      { status: 202 },
    ),
  ),
  http.post("/api/orgs/:slug/deletion-requests/:requestId/cancel", () =>
    HttpResponse.json({
      ...DEFAULT_DELETION_REQUEST,
      state: "cancelled",
      cancelled_by: "u-1",
      cancellation_reason: "request recorded in error",
      cancelled_at: "2026-07-15T12:02:00Z",
      version: 2,
    }),
  ),
  http.get("/api/orgs/:slug/documents/:documentId/legal-holds", () =>
    HttpResponse.json({ items: [] }),
  ),
  http.post("/api/orgs/:slug/documents/:documentId/legal-holds", ({ params }) =>
    HttpResponse.json(
      { ...DEFAULT_LEGAL_HOLD, document_id: String(params["documentId"]) },
      { status: 201 },
    ),
  ),
  http.post("/api/orgs/:slug/legal-holds/:holdId/release", () =>
    HttpResponse.json({
      ...DEFAULT_LEGAL_HOLD,
      state: "released",
      released_by: "u-1",
      release_reason: "preservation requirement ended",
      released_at: "2026-07-15T12:10:00Z",
      version: 2,
    }),
  ),
  http.get("/api/orgs/:slug/documents/:documentId/runs", ({ params }) => {
    const found = DEFAULT_DOCUMENTS.find((d) => d.id === String(params["documentId"]));
    if (!found) {
      return HttpResponse.json({ error: { message: "Document not found." } }, { status: 404 });
    }
    return HttpResponse.json({
      document_id: found.id,
      state: found.state,
      runs: found.state === "failed_retryable" ? DEFAULT_RUNS : [],
    });
  }),
  http.post("/api/orgs/:slug/documents/:documentId/reprocess", ({ params }) =>
    HttpResponse.json({
      id: String(params["documentId"]),
      state: "queued",
      mode: "current_config",
      run_number: 2,
      consequence:
        "A new run will re-execute the full pipeline under the stream's currently published configuration. Previous runs and their artifacts remain unchanged as evidence.",
    }),
  ),
  http.get("/api/orgs/:slug/documents/:documentId", ({ params }) => {
    const found = DEFAULT_DOCUMENTS.find((d) => d.id === String(params["documentId"]));
    if (!found) {
      return HttpResponse.json({ error: { message: "Document not found." } }, { status: 404 });
    }
    if (found.state === "deleted") {
      return HttpResponse.json({
        document: found,
        artifacts: [],
        context: { stream_id: "" },
        timeline: [
          {
            occurred_at: "2026-07-15T12:05:00+00:00",
            action: "document.deletion_completed",
            actor_type: "system",
            actor_id: "system:document-deletion",
            target_type: "document",
            summary: {
              tombstone_id: "f9999999-9999-4999-8999-999999999999",
              object_keys_deleted: 3,
              category_counts: { artifacts: 1, processing_runs: 1, review_tasks: 1 },
            },
            correlation_id: null,
          },
        ],
      });
    }
    return HttpResponse.json({
      document: found,
      artifacts: [
        {
          id: "91111111-1111-4111-8111-111111111111",
          kind: "original",
          sha256: found.content_sha256,
          size_bytes: found.size_bytes,
          content_type: found.content_type,
          produced_by_stage: "intake",
          retention_class: "standard",
          created_at: found.received_at,
        },
      ],
      context: {
        stream_id: found.stream_id,
        stream_slug: "email",
        stream_name: "Email intake",
        stream_version_number: 2,
        pinned_process_version_id: "32222222-2222-4222-8222-222222222222",
      },
      timeline: [
        {
          occurred_at: found.received_at,
          action: "document.received",
          actor_type: "user",
          actor_id: "user:u-1",
          target_type: "document",
          summary: { source_channel: found.source_channel },
          correlation_id: "corr-1",
        },
        {
          occurred_at: found.received_at,
          action: "document.state_changed",
          actor_type: "system",
          actor_id: "system:file-inspection",
          target_type: "document",
          summary: { from: "received", to: "validating_file", reason: null },
          correlation_id: "corr-1",
        },
        {
          occurred_at: found.received_at,
          action: "document.state_changed",
          actor_type: "system",
          actor_id: "system:malware-scan",
          target_type: "document",
          summary: { from: "validating_file", to: found.state, reason: found.state_reason },
          correlation_id: "corr-1",
        },
      ],
    });
  }),
  http.post("/api/orgs/:slug/artifacts/:artifactId/download-url", ({ params }) =>
    HttpResponse.json({
      url: `https://storage.test/signed/${String(params["artifactId"])}`,
      expires_at: "2026-07-13T12:05:00+00:00",
      method: "GET",
    }),
  ),
  // Stored page text served from a "signed" URL (REV-004 text search).
  http.get("https://storage.test/signed/:artifactId", () =>
    HttpResponse.text("PURCHASE ORDER PO-100042 total 1,234.50 Acme Industrial Supply"),
  ),
  http.get("/api/orgs/:slug/documents/:documentId/pages", ({ params }) => {
    const found = DEFAULT_DOCUMENTS.find((d) => d.id === String(params["documentId"]));
    if (!found) {
      return HttpResponse.json({ error: { message: "Document not found." } }, { status: 404 });
    }
    if (found.state !== "failed_retryable") {
      return HttpResponse.json({
        document_id: found.id,
        run_id: null,
        run_number: null,
        pages: [],
      });
    }
    return HttpResponse.json({
      document_id: found.id,
      run_id: "a1111111-1111-4111-8111-111111111111",
      run_number: 1,
      pages: [
        {
          page_number: 1,
          width_px: 1700,
          height_px: 2200,
          dpi: 200,
          rotation_degrees: 0,
          content_type: "image/png",
          image_artifact_id: "c1111111-1111-4111-8111-111111111111",
          text_artifact_id: null,
        },
        {
          page_number: 2,
          width_px: 1700,
          height_px: 2200,
          dpi: 200,
          rotation_degrees: 0,
          content_type: "image/png",
          image_artifact_id: "c2222222-2222-4222-8222-222222222222",
          text_artifact_id: "c3333333-3333-4333-8333-333333333333",
        },
      ],
    });
  }),
  http.get("/api/orgs/:slug/processes/:processSlug/schema", () =>
    HttpResponse.json(DEFAULT_SCHEMA_LISTING),
  ),
  http.get("/api/orgs/:slug/processes/:processSlug/rules", () =>
    HttpResponse.json(DEFAULT_RULES_LISTING),
  ),
  http.post("/api/orgs/:slug/processes/:processSlug/rules/validate", () =>
    HttpResponse.json({ valid: true, message: null }),
  ),
  http.get("/api/orgs/:slug/streams", () => HttpResponse.json(DEFAULT_STREAMS)),
  // Upload flow (ING-008): session -> signed PUT -> complete.
  http.post("/api/orgs/:slug/streams/:streamSlug/uploads", () =>
    HttpResponse.json(
      {
        session_id: "71111111-1111-4111-8111-111111111111",
        document_id: "72222222-2222-4222-8222-222222222222",
        upload_url: "https://storage.test/orgs/org-1/documents/doc/original/token-po.pdf",
        upload_method: "PUT",
        upload_headers: { "x-goog-meta-sha256": "0".repeat(64) },
        expires_at: "2026-07-13T12:00:00+00:00",
        state: "pending",
      },
      { status: 201 },
    ),
  ),
  http.put("https://storage.test/*", () => new HttpResponse(null, { status: 200 })),
  http.post("/api/orgs/:slug/uploads/:sessionId/complete", () =>
    HttpResponse.json({
      document_id: "72222222-2222-4222-8222-222222222222",
      state: "queued",
      state_reason: null,
      duplicate_of: null,
    }),
  ),
  http.post("/api/orgs/:slug/uploads/:sessionId/abort", () =>
    HttpResponse.json({ state: "aborted" }),
  ),
  http.post("/api/orgs/:slug/streams/:streamSlug/resolve", async ({ request }) => {
    const body = (await request.json()) as { overrides: Record<string, unknown> };
    return HttpResponse.json(buildResolvePreview(body.overrides ?? {}));
  }),
  http.get("/api/orgs/:slug/stream-versions/:streamVersionId/instructions", ({ params }) =>
    HttpResponse.json({
      items: [
        {
          id: "instruction-1",
          stream_version_id: String(params["streamVersionId"]),
          schema_version_id: "50000000-0000-4000-8000-000000000001",
          version_number: 1,
          state: "draft",
          reference: "instruction:instruction-1:v1",
          content: {
            instructions: "Extract purchase-order identifiers and totals.",
            field_guidance: { po_number: "Preserve leading zeros." },
          },
          change_summary: "Initial extraction guidance",
          published_at: null,
          published_by: null,
        },
      ],
    }),
  ),
  http.post(
    "/api/orgs/:slug/stream-versions/:streamVersionId/instructions",
    async ({ request, params }) => {
      const body = (await request.json()) as {
        schema_version_id: string;
        content: Record<string, unknown>;
        change_summary: string | null;
      };
      return HttpResponse.json(
        {
          id: "instruction-new",
          stream_version_id: String(params["streamVersionId"]),
          schema_version_id: body.schema_version_id,
          version_number: 2,
          state: "draft",
          reference: "instruction:instruction-new:v2",
          content: body.content,
          change_summary: body.change_summary,
          published_at: null,
          published_by: null,
        },
        { status: 201 },
      );
    },
  ),
  http.patch("/api/orgs/:slug/instructions/:instructionId", async ({ request, params }) => {
    const body = (await request.json()) as {
      content: Record<string, unknown>;
      change_summary: string | null;
    };
    return HttpResponse.json({
      id: String(params["instructionId"]),
      stream_version_id: DEFAULT_STREAM_DRAFT.id,
      schema_version_id: "50000000-0000-4000-8000-000000000001",
      version_number: 1,
      state: "draft",
      reference: `instruction:${String(params["instructionId"])}:v1`,
      content: body.content,
      change_summary: body.change_summary,
      published_at: null,
      published_by: null,
    });
  }),
  http.post("/api/orgs/:slug/instructions/:instructionId/publish", ({ params }) =>
    HttpResponse.json({
      id: String(params["instructionId"]),
      stream_version_id: DEFAULT_STREAM_DRAFT.id,
      schema_version_id: "50000000-0000-4000-8000-000000000001",
      version_number: 1,
      state: "published",
      reference: `instruction:${String(params["instructionId"])}:v1`,
      content: {
        instructions: "Extract purchase-order identifiers and totals.",
        field_guidance: { po_number: "Preserve leading zeros." },
      },
      change_summary: "Initial extraction guidance",
      published_at: "2026-07-15T12:00:00Z",
      published_by: "user:u-1",
    }),
  ),
  http.get("/api/orgs/:slug/catalogs", () =>
    HttpResponse.json({
      items: [
        {
          id: "cat-1",
          name: "Products",
          slug: "products",
          catalog_type: "products",
          source: "csv_import",
          active_version_id: "catv-1",
        },
        {
          id: "cat-2",
          name: "Customers",
          slug: "customers",
          catalog_type: "customers",
          source: "manual",
          active_version_id: null,
        },
      ],
    }),
  ),
  http.get("/api/orgs/:slug/catalogs/:catalogSlug", () =>
    HttpResponse.json({
      catalog: {
        id: "cat-1",
        name: "Products",
        slug: "products",
        catalog_type: "products",
        source: "csv_import",
        active_version_id: "catv-1",
      },
      versions: [
        {
          id: "catv-1",
          version_number: 1,
          state: "published",
          record_count: 2,
          change_summary: "initial import",
          published_at: "2026-07-01T10:00:00Z",
          published_by: "user:u-1",
        },
        {
          id: "catv-2",
          version_number: 2,
          state: "draft",
          record_count: 3,
          change_summary: "CSV import: 3 rows (1 skipped)",
          published_at: null,
          published_by: null,
        },
      ],
    }),
  ),
  http.get("/api/orgs/:slug/catalogs/:catalogSlug/versions/:versionId/records", ({ request }) => {
    const url = new URL(request.url);
    const q = (url.searchParams.get("q") ?? "").toLowerCase();
    const items = [
      {
        id: "rec-1",
        source_id: "SKU-1",
        display_name: "Widget 9mm",
        aliases: ["WIDGET-9"],
        attributes: { uom: "EA" },
        effective_from: "2026-01-01",
        effective_to: null,
      },
      {
        id: "rec-2",
        source_id: "SKU-2",
        display_name: "Flange Kit",
        aliases: [],
        attributes: { uom: "BOX" },
        effective_from: null,
        effective_to: null,
      },
    ].filter(
      (record) =>
        !q ||
        record.source_id.toLowerCase().includes(q) ||
        record.display_name.toLowerCase().includes(q) ||
        record.aliases.some((alias) => alias.toLowerCase().includes(q)),
    );
    return HttpResponse.json({ items, has_more: false, next_cursor: null });
  }),
  http.post("/api/orgs/:slug/catalogs/:catalogSlug/imports", async ({ request }) => {
    const body = (await request.json()) as { dry_run?: boolean; allow_partial?: boolean };
    const base = {
      records: 2,
      issues: [{ row_number: 3, message: "missing 'sku'" }],
      warnings: [],
      encoding: "utf-8",
      preview: { added: ["SKU-3"], changed: ["SKU-1"], deactivated: ["SKU-2"], unchanged: 0 },
    };
    if (body.dry_run) {
      return HttpResponse.json({ ...base, status: "previewed", version: null });
    }
    if (!body.allow_partial) {
      return HttpResponse.json(
        { error: { message: "1 rows failed validation; fix the file or pass allow_partial" } },
        { status: 422 },
      );
    }
    return HttpResponse.json({
      ...base,
      status: "draft_created",
      version: {
        id: "catv-3",
        version_number: 3,
        state: "draft",
        record_count: 2,
        change_summary: null,
        published_at: null,
        published_by: null,
      },
    });
  }),
  http.post("/api/orgs/:slug/catalogs/:catalogSlug/versions/:versionId/activate", ({ params }) =>
    HttpResponse.json({
      id: params.versionId,
      version_number: 2,
      state: "published",
      record_count: 3,
      change_summary: null,
      published_at: "2026-07-13T10:00:00Z",
      published_by: "user:u-1",
    }),
  ),
  http.get("/api/orgs/:slug/providers", () =>
    HttpResponse.json({
      items: [
        {
          name: "pdfium-native-text",
          capability: "native_text",
          languages: ["*"],
          region: "local",
          local: true,
          data_policy: {
            sends_content_to_third_party: false,
            retains_content: false,
            uses_content_for_training: false,
          },
          warnings: ["Runs inside the deployment; content never leaves (local-only safe)."],
          availability: "always",
          description: "Sandboxed digital-PDF text extraction.",
          health: "unknown",
          approved: false,
          credential_configured: false,
          credential_id: null,
        },
        {
          name: "hosted-ocr",
          capability: "ocr",
          languages: ["*"],
          region: "eu",
          local: false,
          data_policy: {
            sends_content_to_third_party: true,
            retains_content: true,
            uses_content_for_training: false,
          },
          warnings: [
            "Customer content LEAVES the deployment to a third party.",
            "The vendor RETAINS customer content after processing.",
          ],
          availability: "requires_endpoint_config",
          description: "Example hosted OCR destination.",
          health: "unknown",
          approved: false,
          credential_configured: false,
          credential_id: null,
        },
        {
          name: "local-openai-compatible",
          capability: "field_extraction",
          languages: ["*"],
          region: "local",
          local: true,
          data_policy: {
            sends_content_to_third_party: false,
            retains_content: false,
            uses_content_for_training: false,
          },
          warnings: ["Runs inside the deployment; content never leaves (local-only safe)."],
          availability: "requires_endpoint_config",
          description: "Local OpenAI-compatible extraction.",
          health: "unknown",
          approved: false,
          credential_configured: false,
          credential_id: null,
        },
        {
          name: "anthropic-claude",
          capability: "field_extraction",
          languages: ["*"],
          region: "us",
          local: false,
          data_policy: {
            sends_content_to_third_party: true,
            retains_content: false,
            uses_content_for_training: false,
          },
          warnings: ["Customer content LEAVES the deployment to a third party."],
          availability: "requires_tenant_credential",
          description: "Hosted Claude extraction.",
          health: "unknown",
          approved: true,
          credential_configured: true,
          credential_id: "cred-anthropic",
        },
      ],
      health_note:
        "Health is derived from persisted worker attempts; providers with no recent attempts remain unknown.",
    }),
  ),
  http.get("/api/orgs/:slug/provider-credentials", () =>
    HttpResponse.json({
      items: [
        {
          id: "cred-anthropic",
          provider_name: "anthropic-claude",
          label: "Production Claude",
          kind: "api_key",
          status: "current",
          created_by: "user:u-1",
          created_at: "2026-07-15T12:00:00Z",
          superseded_at: null,
          revoked_at: null,
          revocation_reason: null,
        },
      ],
    }),
  ),
  http.put("/api/orgs/:slug/providers/:providerName/credential", async ({ params, request }) => {
    const body = (await request.json()) as { label: string };
    return HttpResponse.json({
      credential: {
        id: `cred-${String(params["providerName"])}`,
        provider_name: String(params["providerName"]),
        label: body.label,
        kind: "api_key",
        status: "current",
        created_by: "user:u-1",
        created_at: "2026-07-15T12:00:00Z",
        superseded_at: null,
        revoked_at: null,
        revocation_reason: null,
      },
      rotated: true,
      retained_credential_id: "cred-old",
      detail:
        "The prior value is retained for immutable policy/run pins until explicit revocation.",
    });
  }),
  http.post("/api/orgs/:slug/provider-credentials/:credentialId/revoke", ({ params }) =>
    HttpResponse.json({
      credential: {
        id: String(params["credentialId"]),
        provider_name: "anthropic-claude",
        label: "Production Claude",
        kind: "api_key",
        status: "revoked",
        created_by: "user:u-1",
        created_at: "2026-07-15T12:00:00Z",
        superseded_at: null,
        revoked_at: "2026-07-15T13:00:00Z",
        revocation_reason: "rotated",
      },
      revocation_queued: true,
      affected_policy_ids: [],
      detail: "Revocation queued for durable post-commit execution.",
    }),
  ),
  http.get("/api/orgs/:slug/policies/:policyType", ({ params }) =>
    HttpResponse.json({
      items:
        params["policyType"] === "provider"
          ? [
              {
                id: "provider-policy-draft",
                policy_type: "provider",
                version_number: 2,
                state: "draft",
                definition: {
                  provider_name: "anthropic-claude",
                  credential_id: "cred-anthropic",
                  capabilities: ["ocr", "field_extraction"],
                  allow_third_party_processing: true,
                  allowed_regions: ["us"],
                },
                change_summary: "Claude routing",
                published_at: null,
                published_by: null,
                version: 1,
              },
            ]
          : [],
    }),
  ),
  http.post("/api/orgs/:slug/policies/:policyType/drafts", async ({ params, request }) => {
    const body = (await request.json()) as {
      definition: Record<string, unknown>;
      change_summary: string | null;
    };
    return HttpResponse.json(
      {
        id: `${String(params["policyType"])}-policy-new`,
        policy_type: String(params["policyType"]),
        version_number: 3,
        state: "draft",
        definition: body.definition,
        change_summary: body.change_summary,
        published_at: null,
        published_by: null,
        version: 1,
      },
      { status: 201 },
    );
  }),
  http.post("/api/orgs/:slug/policies/:policyType/:policyId/validate", () =>
    HttpResponse.json({ valid: true, findings: [] }),
  ),
  http.post("/api/orgs/:slug/policies/:policyType/:policyId/publish", ({ params }) =>
    HttpResponse.json({
      id: String(params["policyId"]),
      policy_type: String(params["policyType"]),
      version_number: 2,
      state: "published",
      definition: {},
      change_summary: "Published from test",
      published_at: "2026-07-15T13:00:00Z",
      published_by: "user:u-1",
      version: 2,
    }),
  ),
  http.post("/api/orgs/:slug/providers/routing-preview", async ({ request }) => {
    const body = (await request.json()) as { capability: string; local_only: boolean };
    return HttpResponse.json(
      body.local_only
        ? {
            order: ["pdfium-native-text"],
            explanation: [
              "catalog providers for native_text: pdfium-native-text, hosted-ocr",
              "eliminated hosted-ocr: the policy is local-only",
              "preview order (static catalog; live health/quality/cost apply at runtime): pdfium-native-text",
            ],
          }
        : {
            order: ["hosted-ocr", "pdfium-native-text"],
            explanation: [
              "catalog providers for native_text: pdfium-native-text, hosted-ocr",
              "preview order (static catalog; live health/quality/cost apply at runtime): hosted-ocr, pdfium-native-text",
            ],
          },
    );
  }),
  http.get("/api/orgs/:slug/streams/:streamSlug/simulation", () =>
    HttpResponse.json({
      available: false,
      reason:
        "No evaluation runs are recorded for this stream yet. Evaluations run a candidate configuration against a published gold dataset; results appear here once run storage lands.",
    }),
  ),
  http.get("/api/orgs/:slug/streams/:streamSlug", () => HttpResponse.json(DEFAULT_STREAM_DETAIL)),
  http.get("/api/orgs/:slug/processes", () => HttpResponse.json(DEFAULT_PROCESSES)),
  http.get("/api/orgs/:slug/processes/:processSlug", () =>
    HttpResponse.json(DEFAULT_PROCESS_DETAIL),
  ),
  http.get("/api/orgs/:slug/processes/:processSlug/versions/:versionId", ({ params }) => {
    const versionId = String(params["versionId"]);
    const summary = DEFAULT_PROCESS_DETAIL.versions.find((v) => v.id === versionId);
    const definition = PROCESS_VERSION_DEFINITIONS[versionId];
    if (!summary || !definition) {
      return HttpResponse.json({ error: { message: "Version not found." } }, { status: 404 });
    }
    return HttpResponse.json({ ...summary, definition });
  }),
  http.post("/api/organizations", async ({ request }) => {
    const body = (await request.json()) as { name: string; slug: string };
    return HttpResponse.json(
      { id: "org-created", name: body.name, slug: body.slug, status: "active", version: 1 },
      { status: 201 },
    );
  }),
  http.post("/api/invitations/accept", async ({ request }) => {
    const body = (await request.json()) as { organization_slug: string };
    return HttpResponse.json({
      membership_id: `membership-${body.organization_slug}`,
      user_id: "u-1",
      invited_email: "reviewer@northstar.example",
      status: "active",
      version: 2,
      assigned_roles: [],
    });
  }),
  http.get("/api/orgs/:slug/members", () =>
    HttpResponse.json({
      items: [
        {
          membership_id: "m-1",
          user_id: "u-1",
          invited_email: "reviewer@northstar.example",
          status: "active",
          version: 2,
          assigned_roles: [
            {
              id: "role-admin",
              name: "Organization admin",
              slug: "org-admin",
              is_system: true,
            },
          ],
        },
        {
          membership_id: "m-invited",
          user_id: null,
          invited_email: "new.member@example.com",
          status: "invited",
          version: 1,
          assigned_roles: [],
        },
      ],
      has_more: false,
      next_cursor: null,
    }),
  ),
  http.post("/api/orgs/:slug/invitations", async ({ request }) => {
    const body = (await request.json()) as { email: string };
    return HttpResponse.json(
      { membership_id: "m-new", email: body.email, status: "invited", created: true },
      { status: 201 },
    );
  }),
  http.patch("/api/orgs/:slug/members/:membershipId", async ({ request, params }) => {
    const body = (await request.json()) as { status: string };
    return HttpResponse.json({
      membership_id: String(params["membershipId"]),
      user_id: "u-1",
      invited_email: "reviewer@northstar.example",
      status: body.status,
      version: 3,
      assigned_roles: [],
    });
  }),
  http.get("/api/orgs/:slug/roles", () =>
    HttpResponse.json([
      {
        id: "role-admin",
        name: "Organization admin",
        slug: "org-admin",
        is_system: true,
        permissions: ["organization.manage", "members.manage", "roles.manage"],
      },
      {
        id: "role-reviewer",
        name: "Reviewer",
        slug: "reviewer",
        is_system: true,
        permissions: ["documents.read", "documents.review"],
      },
    ]),
  ),
  http.post("/api/orgs/:slug/roles", async ({ request }) => {
    const body = (await request.json()) as { name: string; slug: string; permissions: string[] };
    return HttpResponse.json({ id: "role-created", ...body, is_system: false }, { status: 201 });
  }),
  http.post("/api/orgs/:slug/members/:membershipId/roles", async ({ request, params }) => {
    const body = (await request.json()) as { role_slug: string };
    return HttpResponse.json(
      { membership_id: String(params["membershipId"]), role_slug: body.role_slug },
      { status: 201 },
    );
  }),
  http.get("/api/orgs/:slug/members/:membershipId/roles", ({ params }) =>
    HttpResponse.json(
      String(params["membershipId"]) === "m-1"
        ? [
            {
              id: "role-admin",
              name: "Organization admin",
              slug: "org-admin",
              is_system: true,
              permissions: ["organization.manage", "members.manage", "roles.manage"],
            },
          ]
        : [],
    ),
  ),
  http.delete("/api/orgs/:slug/members/:membershipId/roles/:roleSlug", ({ params }) =>
    HttpResponse.json({
      membership_id: String(params["membershipId"]),
      role_slug: String(params["roleSlug"]),
      revoked: true,
    }),
  ),
  http.get("/api/orgs/:slug/service-credentials", () => HttpResponse.json({ items: [] })),
  http.post("/api/orgs/:slug/service-credentials", async ({ request }) => {
    const body = (await request.json()) as {
      name: string;
      allowed_stream_ids: string[];
    };
    return HttpResponse.json(
      {
        credential: {
          id: "service-credential-1",
          name: body.name,
          key_prefix: "abc12345",
          scopes: ["documents.upload"],
          allowed_stream_ids: body.allowed_stream_ids,
          status: "active",
          expires_at: "2026-10-13T12:00:00Z",
          last_used_at: null,
          created_by: "user:u-1",
          created_at: "2026-07-15T12:00:00Z",
          updated_at: "2026-07-15T12:00:00Z",
          version: 1,
        },
        api_key: "soa_abc12345_one-time-secret",
        warning: "Copy this API key now. It cannot be retrieved after this response.",
      },
      { status: 201 },
    );
  }),
  http.post("/api/orgs/:slug/service-credentials/:credentialId/rotate", ({ params }) =>
    HttpResponse.json({
      credential: {
        id: String(params["credentialId"]),
        name: "Warehouse connector",
        key_prefix: "def67890",
        scopes: ["documents.upload"],
        allowed_stream_ids: [DEFAULT_STREAMS[0].id],
        status: "active",
        expires_at: "2026-10-13T12:00:00Z",
        last_used_at: null,
        created_by: "user:u-1",
        created_at: "2026-07-15T12:00:00Z",
        updated_at: "2026-07-15T13:00:00Z",
        version: 2,
      },
      api_key: "soa_def67890_rotated-one-time-secret",
      warning: "Copy this API key now. The previous key stopped working immediately.",
    }),
  ),
  http.post("/api/orgs/:slug/service-credentials/:credentialId/revoke", ({ params }) =>
    HttpResponse.json({
      credential: {
        id: String(params["credentialId"]),
        name: "Warehouse connector",
        key_prefix: "abc12345",
        scopes: ["documents.upload"],
        allowed_stream_ids: [DEFAULT_STREAMS[0].id],
        status: "revoked",
        expires_at: "2026-10-13T12:00:00Z",
        last_used_at: null,
        created_by: "user:u-1",
        created_at: "2026-07-15T12:00:00Z",
        updated_at: "2026-07-15T13:00:00Z",
        version: 2,
      },
    }),
  ),
  http.get("/api/orgs/:slug/data-exports", () => HttpResponse.json({ items: [] })),
  http.post("/api/orgs/:slug/data-exports", () =>
    HttpResponse.json(
      {
        id: "export-new",
        scope: "organization",
        snapshot_at: "2026-07-15T12:00:00Z",
        state: "pending",
        total_documents: 0,
        processed_documents: 0,
        progress: 0,
        total_records: 0,
        safe_error: null,
        expires_at: "2026-07-16T12:00:00Z",
        manifest_download_url: null,
        manifest_expires_at: null,
        parts: [],
      },
      { status: 202 },
    ),
  ),
  http.post("/api/orgs/:slug/data-exports/:exportId/cancel", async ({ request, params }) => {
    const body = (await request.json()) as { reason: string };
    return HttpResponse.json({
      id: String(params["exportId"]),
      scope: "organization",
      snapshot_at: "2026-07-15T12:00:00Z",
      state: "cancelled",
      total_documents: 10,
      processed_documents: 2,
      progress: 0.2,
      total_records: 20,
      safe_error: `cancelled by operator: ${body.reason}`,
      expires_at: "2026-07-16T12:00:00Z",
      manifest_download_url: null,
      manifest_expires_at: null,
      parts: [],
    });
  }),
  http.get("/api/me", () => HttpResponse.json(DEFAULT_ME)),
  http.get("/api/orgs/:slug/jobs/stats", () => HttpResponse.json(DEFAULT_JOB_STATS)),
  http.get("/api/orgs/:slug/jobs", ({ request }) => {
    const url = new URL(request.url);
    const status = url.searchParams.get("job_status");
    if (!status) {
      return HttpResponse.json(DEFAULT_JOBS);
    }
    return HttpResponse.json({
      ...DEFAULT_JOBS,
      items: DEFAULT_JOBS.items.filter((job) => job.status === status),
    });
  }),
  // Mutations return a valid JobSummary shape so tests exercising the
  // happy path without overriding these stay pinned to the real contract.
  http.post("/api/orgs/:slug/jobs/:jobId/replay", ({ params }) =>
    HttpResponse.json({
      ...DEFAULT_JOBS.items[1],
      id: String(params["jobId"]),
      status: "pending",
      attempts: 0,
    }),
  ),
  http.post("/api/orgs/:slug/jobs/:jobId/cancel", ({ params }) =>
    HttpResponse.json({
      ...DEFAULT_JOBS.items[0],
      id: String(params["jobId"]),
      status: "cancelled",
    }),
  ),
];

export const server = setupServer(...handlers);
