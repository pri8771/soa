import { HttpResponse, delay, http } from "msw";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import {
  DEFAULT_DELETION_REQUEST,
  DEFAULT_DOCUMENTS,
  DEFAULT_LEGAL_HOLD,
  DEFAULT_ME,
  server,
} from "../test/msw";
import { renderApp } from "../test/render";

const QUEUED = DEFAULT_DOCUMENTS[0];
const QUARANTINED = DEFAULT_DOCUMENTS[2];
const FAILED = DEFAULT_DOCUMENTS[3];
const DELETED = DEFAULT_DOCUMENTS[4];

describe("Document detail (ING-012)", () => {
  it("is a stable deep link with summary, files, live panels, and timeline", async () => {
    await renderApp(`/app/northstar/documents/${QUEUED.id}`);
    expect(await screen.findByRole("heading", { name: "po-4711.pdf" })).toBeInTheDocument();

    const summary = screen.getByRole("region", { name: "Summary" });
    expect(within(summary).getByText("queued")).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: "Email intake" })).toBeInTheDocument();
    expect(within(summary).getByText(/configuration v2/)).toBeInTheDocument();

    const files = screen.getByRole("region", { name: "Files" });
    expect(within(files).getByText("original")).toBeInTheDocument();
    expect(within(files).getByRole("button", { name: "Download original" })).toBeInTheDocument();

    // State-dependent panels are honest when their live data is not available yet.
    expect(screen.getByText(/open the task from the Review queue/)).toBeInTheDocument();
    expect(
      screen.getByText(/canonical order is created when the document is approved/),
    ).toBeInTheDocument();
    expect(await screen.findByText(/no deliveries yet/)).toBeInTheDocument();
    // No runs yet: the processing panel says so instead of inventing one.
    const processing = screen.getByRole("region", { name: "Processing" });
    expect(within(processing).getByText(/No processing runs yet/)).toBeInTheDocument();

    const timeline = screen.getByRole("region", { name: "Timeline" });
    expect(within(timeline).getByText("document.received")).toBeInTheDocument();
    expect(within(timeline).getByText("received → validating file")).toBeInTheDocument();
  });

  it("offers cancel while the document is active", async () => {
    await renderApp(`/app/northstar/documents/${QUEUED.id}`);
    await screen.findByRole("heading", { name: "po-4711.pdf" });
    expect(screen.getByRole("button", { name: "Cancel document" })).toBeInTheDocument();
  });

  it("offers no cancel once the document is settled", async () => {
    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    await screen.findByRole("heading", { name: "invoice-evil.pdf" });
    expect(screen.queryByRole("button", { name: "Cancel document" })).not.toBeInTheDocument();
  });

  it("cancelling requires a reason and posts it", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/documents/:documentId/cancel", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ id: QUEUED.id, state: "cancelled" });
      }),
    );
    await renderApp(`/app/northstar/documents/${QUEUED.id}`);
    await screen.findByRole("heading", { name: "po-4711.pdf" });
    await user.click(screen.getByRole("button", { name: "Cancel document" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Cancel document" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Reason/), "wrong stream");
    await user.click(confirm);
    await waitFor(() => expect(posted).toEqual({ reason: "wrong stream" }));
  });

  it("offers an approval-gated deletion request for a settled document", async () => {
    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    await screen.findByRole("heading", { name: "invoice-evil.pdf" });
    expect(await screen.findByRole("button", { name: "Request deletion" })).toBeInTheDocument();
  });

  it("requesting deletion requires a reason and posts it", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    server.use(
      http.post(
        "/api/orgs/northstar/documents/:documentId/deletion-requests",
        async ({ request }) => {
          posted = await request.json();
          return HttpResponse.json(
            {
              ...DEFAULT_DELETION_REQUEST,
              document_id: QUARANTINED.id,
              reason: "customer erasure request",
            },
            { status: 202 },
          );
        },
      ),
    );
    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    await screen.findByRole("heading", { name: "invoice-evil.pdf" });
    await user.click(await screen.findByRole("button", { name: "Request deletion" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Request deletion" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Reason/), "customer erasure request");
    await user.click(confirm);
    await waitFor(() => expect(posted).toEqual({ reason: "customer erasure request" }));
  });

  it("shows durable deletion status and blocks visible self-approval", async () => {
    server.use(
      http.get("/api/orgs/northstar/documents/:documentId/deletion-request", () =>
        HttpResponse.json(DEFAULT_DELETION_REQUEST),
      ),
    );

    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    const deletion = await screen.findByRole("region", { name: "Deletion request" });
    expect(await within(deletion).findByText("pending approval")).toBeInTheDocument();
    expect(within(deletion).getByText("customer erasure request")).toBeInTheDocument();
    expect(within(deletion).getByText(/You submitted this request/)).toBeInTheDocument();
    expect(within(deletion).getByRole("button", { name: "Approve deletion" })).toBeDisabled();
    expect(
      within(deletion).getByRole("button", { name: "Cancel deletion request" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request deletion" })).not.toBeInTheDocument();
  });

  it("allows an independent approver to approve with a recorded reason", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    const independent = { ...DEFAULT_DELETION_REQUEST, requested_by: "u-2" };
    server.use(
      http.get("/api/orgs/northstar/documents/:documentId/deletion-request", () =>
        HttpResponse.json(independent),
      ),
      http.post("/api/orgs/northstar/deletion-requests/:requestId/approve", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json(
          {
            ...independent,
            state: "approved",
            approved_by: "u-1",
            approval_reason: "authority and scope verified",
            approved_at: "2026-07-15T12:02:00Z",
            version: 2,
          },
          { status: 202 },
        );
      }),
    );

    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    const deletion = await screen.findByRole("region", { name: "Deletion request" });
    await user.click(await within(deletion).findByRole("button", { name: "Approve deletion" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Approve deletion" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Reason/), "authority and scope verified");
    await user.click(confirm);

    await waitFor(() => expect(posted).toEqual({ reason: "authority and scope verified" }));
    expect(await within(deletion).findByText("approved")).toBeInTheDocument();
    expect(within(deletion).getByText("Deletion approved")).toBeInTheDocument();
  });

  it("allows a requester to cancel the durable lifecycle with a reason", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    server.use(
      http.get("/api/orgs/northstar/documents/:documentId/deletion-request", () =>
        HttpResponse.json(DEFAULT_DELETION_REQUEST),
      ),
      http.post("/api/orgs/northstar/deletion-requests/:requestId/cancel", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          ...DEFAULT_DELETION_REQUEST,
          state: "cancelled",
          cancelled_by: "u-1",
          cancellation_reason: "request recorded against the wrong record",
          cancelled_at: "2026-07-15T12:03:00Z",
          version: 2,
        });
      }),
    );

    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    const deletion = await screen.findByRole("region", { name: "Deletion request" });
    await user.click(
      await within(deletion).findByRole("button", { name: "Cancel deletion request" }),
    );
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Cancel deletion request" });
    await user.type(
      within(dialog).getByLabelText(/Reason/),
      "request recorded against the wrong record",
    );
    await user.click(confirm);

    await waitFor(() =>
      expect(posted).toEqual({ reason: "request recorded against the wrong record" }),
    );
    expect(await within(deletion).findByText("cancelled")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Request deletion" })).toBeInTheDocument();
  });

  it("hides approval and hold controls without their permissions", async () => {
    server.use(
      http.get("/api/me", () =>
        HttpResponse.json({
          ...DEFAULT_ME,
          memberships: [
            {
              ...DEFAULT_ME.memberships[0],
              permissions: DEFAULT_ME.memberships[0].permissions.filter(
                (permission) =>
                  permission !== "data.delete.approve" && permission !== "data.retention.manage",
              ),
            },
          ],
        }),
      ),
      http.get("/api/orgs/northstar/documents/:documentId/deletion-request", () =>
        HttpResponse.json({ ...DEFAULT_DELETION_REQUEST, requested_by: "u-2" }),
      ),
    );

    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    const deletion = await screen.findByRole("region", { name: "Deletion request" });
    expect(
      within(deletion).queryByRole("button", { name: "Approve deletion" }),
    ).not.toBeInTheDocument();
    expect(
      await within(deletion).findByRole("button", { name: "Cancel deletion request" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Legal hold" })).not.toBeInTheDocument();
  });

  it("places a legal hold with a reason", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/documents/:documentId/legal-holds", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json(
          { ...DEFAULT_LEGAL_HOLD, reason: "preserve for active litigation" },
          { status: 201 },
        );
      }),
    );

    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    const holds = await screen.findByRole("region", { name: "Legal hold" });
    await user.click(await within(holds).findByRole("button", { name: "Place legal hold" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Place legal hold" });
    await user.type(within(dialog).getByLabelText(/Reason/), "preserve for active litigation");
    await user.click(confirm);

    await waitFor(() => expect(posted).toEqual({ reason: "preserve for active litigation" }));
    expect(await within(holds).findByText("Active legal hold")).toBeInTheDocument();
    expect(within(holds).getByText("preserve for active litigation")).toBeInTheDocument();
  });

  it("blocks approval on an active hold and releases the hold with a reason", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    server.use(
      http.get("/api/orgs/northstar/documents/:documentId/deletion-request", () =>
        HttpResponse.json({ ...DEFAULT_DELETION_REQUEST, requested_by: "u-2" }),
      ),
      http.get("/api/orgs/northstar/documents/:documentId/legal-holds", () =>
        HttpResponse.json({ items: [DEFAULT_LEGAL_HOLD] }),
      ),
      http.post("/api/orgs/northstar/legal-holds/:holdId/release", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          ...DEFAULT_LEGAL_HOLD,
          state: "released",
          released_by: "u-1",
          release_reason: "preservation period ended",
          released_at: "2026-07-15T12:10:00Z",
          version: 2,
        });
      }),
    );

    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    const holds = await screen.findByRole("region", { name: "Legal hold" });
    expect(await within(holds).findByText("Active legal hold")).toBeInTheDocument();
    const deletion = await screen.findByRole("region", { name: "Deletion request" });
    expect(within(deletion).getByText("Legal hold blocks deletion")).toBeInTheDocument();
    expect(within(deletion).getByRole("button", { name: "Approve deletion" })).toBeDisabled();

    await user.click(within(holds).getByRole("button", { name: "Release legal hold" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByLabelText(/Reason/), "preservation period ended");
    await user.click(within(dialog).getByRole("button", { name: "Release legal hold" }));

    await waitFor(() => expect(posted).toEqual({ reason: "preservation period ended" }));
    expect(
      await within(holds).findByRole("button", { name: "Place legal hold" }),
    ).toBeInTheDocument();
  });

  it("polls detail while deletion is running and converges to the tombstone", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let detailCalls = 0;
    try {
      server.use(
        http.get("/api/orgs/northstar/documents/:documentId/deletion-request", () =>
          HttpResponse.json({
            ...DEFAULT_DELETION_REQUEST,
            requested_by: "u-2",
            state: "running",
            approved_by: "u-3",
            approval_reason: "independent approval",
            approved_at: "2026-07-15T12:02:00Z",
            version: 3,
          }),
        ),
        http.get("/api/orgs/northstar/documents/:documentId", () => {
          detailCalls += 1;
          if (detailCalls === 1) {
            return HttpResponse.json({
              document: QUARANTINED,
              artifacts: [],
              context: { stream_id: QUARANTINED.stream_id },
              timeline: [],
            });
          }
          return HttpResponse.json({
            document: { ...DELETED, id: QUARANTINED.id },
            artifacts: [],
            context: { stream_id: "" },
            timeline: [],
            deletion_tombstone: {
              completed_at: "2026-07-15T12:05:00Z",
              object_keys_deleted: 2,
              category_counts: { artifacts: 1, processing_runs: 1 },
            },
          });
        }),
      );

      await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
      expect(await screen.findByRole("heading", { name: "invoice-evil.pdf" })).toBeInTheDocument();
      expect(await screen.findByText("Deletion in progress")).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(4100);
      });

      expect(await screen.findByRole("heading", { name: "Deleted document" })).toBeInTheDocument();
      expect(detailCalls).toBeGreaterThanOrEqual(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("reduces deleted detail to a counts-only tombstone and purges document caches", async () => {
    const secretFilename = "customer-secret-4709.pdf";
    server.use(
      http.get("/api/orgs/northstar/documents/:documentId", async () => {
        await delay(50);
        return HttpResponse.json({
          document: {
            ...DELETED,
            original_filename: secretFilename,
            content_sha256: "f".repeat(64),
            source_channel: "private-inbox",
            state_reason: "customer name and case details",
          },
          artifacts: [
            {
              id: "artifact-secret",
              kind: "original",
              sha256: "f".repeat(64),
              size_bytes: 500,
              content_type: "application/pdf",
              produced_by_stage: "intake",
              retention_class: "standard",
              created_at: "2026-07-11T09:00:00Z",
            },
          ],
          context: { stream_id: "secret-stream", stream_name: "Secret customer stream" },
          deletion_tombstone: {
            completed_at: "2026-07-15T12:06:00Z",
            object_keys_deleted: 7,
            category_counts: { artifacts: 2, processing_runs: 1, review_tasks: 1 },
          },
          timeline: [
            {
              occurred_at: "2026-07-11T09:00:00Z",
              action: "document.received",
              actor_type: "user",
              actor_id: "customer@example.test",
              target_type: "document",
              summary: { filename: secretFilename },
              correlation_id: "secret-correlation",
            },
          ],
        });
      }),
    );

    const { queryClient } = await renderApp(`/app/northstar/documents/${DELETED.id}`);
    queryClient.setQueryData(["canonical-payload", "northstar", DELETED.id], {
      document_id: DELETED.id,
      payload: { customer: "secret" },
    });
    queryClient.setQueryData(["review-workspace", "northstar", "task-secret"], {
      document: { id: DELETED.id, original_filename: secretFilename },
    });
    queryClient.getMutationCache().build(queryClient, {
      mutationKey: ["download-document-artifact", "northstar", DELETED.id],
      mutationFn: async () => ({ url: "https://storage.test/signed/secret" }),
    });

    expect(await screen.findByRole("heading", { name: "Deleted document" })).toBeInTheDocument();
    expect(screen.getByText("This document’s data has been deleted")).toBeInTheDocument();
    const tombstone = screen.getByRole("region", { name: "Deletion tombstone" });
    expect(within(tombstone).getByText(DELETED.id)).toBeInTheDocument();
    expect(within(tombstone).getByText("7", { selector: "dd" })).toBeInTheDocument();
    expect(within(tombstone).getByText("4", { selector: "dd" })).toBeInTheDocument();

    expect(screen.queryByText(secretFilename)).not.toBeInTheDocument();
    expect(screen.queryByText("Secret customer stream")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Summary" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Files" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Preview" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Processing" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Canonical order" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Delivery" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Timeline" })).not.toBeInTheDocument();

    await waitFor(() => {
      expect(
        queryClient.getQueryData(["canonical-payload", "northstar", DELETED.id]),
      ).toBeUndefined();
      expect(
        queryClient.getQueryData(["review-workspace", "northstar", "task-secret"]),
      ).toBeUndefined();
      expect(
        queryClient
          .getMutationCache()
          .getAll()
          .some((mutation) => mutation.options.mutationKey?.includes(DELETED.id)),
      ).toBe(false);
    });
    expect(
      JSON.stringify(queryClient.getQueryData(["document", "northstar", DELETED.id])),
    ).not.toContain(secretFilename);
  });

  it("shows the processing timeline: stages, attempts, provider, latency, warnings, safe errors", async () => {
    await renderApp(`/app/northstar/documents/${FAILED.id}`);
    await screen.findByRole("heading", { name: "po-4713.pdf" });
    const run = await screen.findByRole("region", { name: "Run 1" });
    expect(within(run).getByRole("heading", { name: "Run 1" })).toBeInTheDocument();
    // Two attempts of the failing stage, each with its own record
    // (preprocessing also ran once, so "attempt 1" appears twice).
    expect(within(run).getAllByText("attempt 1").length).toBe(2);
    expect(within(run).getByText("attempt 2")).toBeInTheDocument();
    expect(within(run).getAllByText("mock").length).toBe(2); // provider
    expect(within(run).getByText("900 ms")).toBeInTheDocument(); // latency
    expect(within(run).getAllByText(/mock provider configured to fail \(retryable\)/).length).toBe(
      2,
    ); // the SAFE error string with its classification
    expect(within(run).getByText("provider responded slowly before failing")).toBeInTheDocument();
    expect(within(run).getByText("ffffffffffff…")).toBeInTheDocument(); // pinned config
    expect(within(run).getByText("cccccccccccc…")).toBeInTheDocument(); // execution contract
    expect(within(run).getByText("rrrrrrrrrrrr…")).toBeInTheDocument(); // sanitized runtime
  });

  it("offers retry controls with the consequence spelled out, and posts the request", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/documents/:documentId/reprocess", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          id: FAILED.id,
          state: "queued",
          mode: "retry",
          run_number: 2,
          consequence: "A new run will re-execute the full pipeline under the same configuration.",
        });
      }),
    );
    await renderApp(`/app/northstar/documents/${FAILED.id}`);
    await screen.findByRole("heading", { name: "po-4713.pdf" });
    await user.click(await screen.findByRole("button", { name: "Retry (same configuration)" }));
    const dialog = await screen.findByRole("dialog");
    // The consequence is explained BEFORE the user confirms.
    expect(within(dialog).getByText(/SAME configuration as the last run/)).toBeInTheDocument();
    expect(within(dialog).getByText(/remain unchanged as evidence/)).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Retry" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Reason/), "transient provider outage");
    await user.click(confirm);
    await waitFor(() =>
      expect(posted).toEqual({ mode: "retry", reason: "transient provider outage" }),
    );
    // ...and afterwards the server's own consequence is on screen.
    expect(await screen.findByText("Run 2 queued")).toBeInTheDocument();
  });

  it("offers no retry controls for active documents", async () => {
    await renderApp(`/app/northstar/documents/${QUEUED.id}`);
    await screen.findByRole("heading", { name: "po-4711.pdf" });
    expect(
      screen.queryByRole("button", { name: "Retry (same configuration)" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Reprocess (current configuration)" }),
    ).not.toBeInTheDocument();
  });

  it("surfaces a refused download (quarantined) without opening anything", async () => {
    const user = userEvent.setup();
    const opened: string[] = [];
    vi.spyOn(window, "open").mockImplementation((url) => {
      opened.push(String(url));
      return null;
    });
    server.use(
      http.post("/api/orgs/northstar/artifacts/:artifactId/download-url", () =>
        HttpResponse.json(
          { error: { message: "This document is quarantined; its files cannot be downloaded." } },
          { status: 403 },
        ),
      ),
    );
    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    await screen.findByRole("heading", { name: "invoice-evil.pdf" });
    await user.click(screen.getByRole("button", { name: "Download original" }));
    expect(await screen.findByText("Download refused")).toBeInTheDocument();
    expect(screen.getByText(/quarantined; its files cannot/)).toBeInTheDocument();
    expect(opened).toEqual([]);
  });
});
