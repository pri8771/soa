import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_DOCUMENTS, server } from "../test/msw";
import { renderApp } from "../test/render";

const QUEUED = DEFAULT_DOCUMENTS[0];
const QUARANTINED = DEFAULT_DOCUMENTS[2];
const FAILED = DEFAULT_DOCUMENTS[3];

describe("Document detail (ING-012)", () => {
  it("is a stable deep link with summary, files, honest placeholders, and timeline", async () => {
    await renderApp(`/app/northstar/documents/${QUEUED.id}`);
    expect(await screen.findByRole("heading", { name: "po-4711.pdf" })).toBeInTheDocument();

    const summary = screen.getByRole("region", { name: "Summary" });
    expect(within(summary).getByText("queued")).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: "Email intake" })).toBeInTheDocument();
    expect(within(summary).getByText(/configuration v2/)).toBeInTheDocument();

    const files = screen.getByRole("region", { name: "Files" });
    expect(within(files).getByText("original")).toBeInTheDocument();
    expect(within(files).getByRole("button", { name: "Download original" })).toBeInTheDocument();

    // Unbuilt/deferred panels are honest, never fake data.
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
