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

export const handlers = [
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
