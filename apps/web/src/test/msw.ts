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
export const RESOLVE_PROCESS = { language: "de" };

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
];

export const handlers = [
  http.get("/api/orgs/:slug/documents", ({ request }) => {
    const url = new URL(request.url);
    const state = url.searchParams.get("document_state");
    const channel = url.searchParams.get("source_channel");
    const search = url.searchParams.get("search");
    let items = DEFAULT_DOCUMENTS;
    if (state) items = items.filter((d) => d.state === state);
    if (channel) items = items.filter((d) => d.source_channel === channel);
    if (search) items = items.filter((d) => d.original_filename.includes(search));
    return HttpResponse.json({ items, has_more: false, next_cursor: null });
  }),
  http.post("/api/orgs/:slug/documents/:documentId/cancel", ({ params }) =>
    HttpResponse.json({ id: String(params["documentId"]), state: "cancelled" }),
  ),
  http.get("/api/orgs/:slug/documents/:documentId", ({ params }) => {
    const found = DEFAULT_DOCUMENTS.find((d) => d.id === String(params["documentId"]));
    if (!found) {
      return HttpResponse.json({ error: { message: "Document not found." } }, { status: 404 });
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
  http.post("/api/orgs/:slug/artifacts/:artifactId/download-url", () =>
    HttpResponse.json({
      url: "https://storage.test/signed/download",
      expires_at: "2026-07-13T12:05:00+00:00",
      method: "GET",
    }),
  ),
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
