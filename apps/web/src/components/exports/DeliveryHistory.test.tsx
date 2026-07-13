import { HttpResponse, http } from "msw";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../../test/msw";
import { DeliveryHistory } from "./DeliveryHistory";

const JOB = {
  id: "f1111111-1111-4111-8111-111111111111",
  document_id: "81111111-1111-4111-8111-111111111111",
  run_id: "a1111111-1111-4111-8111-111111111111",
  canonical_payload_id: "c1111111-1111-4111-8111-111111111111",
  integration_id: "e1111111-1111-4111-8111-111111111111",
  integration_slug: "erp",
  integration_name: "Northstar ERP webhook",
  mapping_version_id: "e2222222-2222-4222-8222-222222222222",
  business_key: "export:i:d:r",
  state: "failed_terminal",
  attempt_count: 1,
  last_error: "receiver returned 400",
  created_at: "2026-07-13T08:00:00+00:00",
  updated_at: "2026-07-13T08:01:00+00:00",
};

const DETAIL = {
  job: JOB,
  mapping_version_number: 2,
  attempts: [
    {
      attempt_number: 1,
      outcome: "terminal_error",
      response_status: 400,
      safe_error: "receiver returned 400",
      request_sha256: "a".repeat(64),
      started_at: "2026-07-13T08:00:00+00:00",
      finished_at: "2026-07-13T08:00:01+00:00",
    },
  ],
};

function renderHistory(canReplay: boolean) {
  server.use(
    http.get("/api/orgs/northstar/exports", () => HttpResponse.json({ items: [JOB] })),
    http.get(`/api/orgs/northstar/exports/${JOB.id}`, () => HttpResponse.json(DETAIL)),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <DeliveryHistory
        organizationSlug="northstar"
        documentId={JOB.document_id}
        canReplay={canReplay}
      />
    </QueryClientProvider>,
  );
}

describe("Delivery history (EXP-009)", () => {
  it("shows jobs, attempts, mapping version, and safe errors — no secret headers", async () => {
    renderHistory(true);
    const row = await screen.findByRole("listitem", {
      name: "Export via Northstar ERP webhook",
    });
    expect(within(row).getByText("failed terminal")).toBeInTheDocument();
    expect(await within(row).findByText("mapping v2")).toBeInTheDocument();
    expect(within(row).getByText(/attempt 1: terminal error \(HTTP 400\)/)).toBeInTheDocument();
    expect(within(row).getByText(/payload sha aaaaaaaaaaaa/)).toBeInTheDocument();
    // Nothing signature- or secret-shaped is rendered.
    expect(document.body.textContent).not.toMatch(/X-SOA-Signature|whsec|Authorization/);
  });

  it("replay requires a reason and posts it", async () => {
    const user = userEvent.setup();
    const posted: Record<string, unknown>[] = [];
    server.use(
      http.post(`/api/orgs/northstar/exports/${JOB.id}/replay`, async ({ request }) => {
        posted.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({ ...JOB, state: "pending" });
      }),
    );
    renderHistory(true);
    const replay = await screen.findByRole("button", { name: "Replay delivery" });
    expect(replay).toBeDisabled(); // no reason yet
    await user.type(screen.getByLabelText(/Replay reason/), "receiver contract fixed");
    await user.click(replay);
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({ reason: "receiver contract fixed" });
    expect(await screen.findByText(/Replay queued with the audited reason/)).toBeInTheDocument();
  });

  it("controls are permission-separated with the reason visible", async () => {
    renderHistory(false);
    const replay = await screen.findByRole("button", { name: "Replay delivery" });
    expect(replay).toBeDisabled();
    expect(
      screen.getByText(/Replaying needs the integrations.replay permission/),
    ).toBeInTheDocument();
  });
});
