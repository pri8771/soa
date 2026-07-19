import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_JOB_STATS, DEFAULT_ME, server } from "../test/msw";
import { renderApp } from "../test/render";

describe("JobsQueue (JOB-007)", () => {
  it("shows queue depth, oldest age, and job rows with safe failure reasons", async () => {
    await renderApp("/app/northstar/jobs");
    expect(await screen.findByText("Queue depth")).toBeInTheDocument();
    // pending 4 + running 1 (async: stats render 0 until the query lands)
    expect(await screen.findByText("5")).toBeInTheDocument();
    expect(await screen.findByText("document.extract")).toBeInTheDocument();
    expect(screen.getByText("delivery endpoint returned 503")).toBeInTheDocument();
    // Attempts stay visible for dead-letter triage.
    expect(screen.getByText("5/5")).toBeInTheDocument();
  });

  it("keeps the status filter in the URL so refreshes cannot reset it", async () => {
    const user = userEvent.setup();
    const { router } = await renderApp("/app/northstar/jobs");
    await screen.findByText("document.extract");

    await user.click(screen.getByRole("button", { name: /Status/ }));
    await user.click(await screen.findByRole("option", { name: "Dead letter" }));

    await waitFor(() =>
      expect((router.state.location.search as { jobStatus?: string }).jobStatus).toBe(
        "dead_letter",
      ),
    );
    await waitFor(() => expect(screen.queryByText("document.extract")).not.toBeInTheDocument());
    expect(screen.getByText("export.webhook")).toBeInTheDocument();
  });

  it("replays a dead-letter job with a required audited reason", async () => {
    const user = userEvent.setup();
    let replayBody: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/jobs/:jobId/replay", async ({ request }) => {
        replayBody = await request.json();
        return HttpResponse.json({});
      }),
    );
    await renderApp("/app/northstar/jobs");
    await screen.findByText("export.webhook");

    await user.click(screen.getByRole("button", { name: "Replay" }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Replay job" });
    expect(confirm).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/Reason/), "provider recovered");
    await user.click(confirm);

    await waitFor(() => expect(replayBody).toEqual({ reason: "provider recovered" }));
  });

  it("shows a Worker stalled badge when pending work has no recent claim", async () => {
    server.use(
      http.get("/api/orgs/:slug/jobs/stats", () =>
        HttpResponse.json({
          ...DEFAULT_JOB_STATS,
          last_claim_at: new Date(Date.now() - 10 * 60_000).toISOString(),
        }),
      ),
    );
    await renderApp("/app/northstar/jobs");
    expect(await screen.findByText("Worker")).toBeInTheDocument();
    expect(screen.getByText("Stalled")).toBeInTheDocument();
  });

  it("shows a Worker stalled badge when no job has ever been claimed", async () => {
    server.use(
      http.get("/api/orgs/:slug/jobs/stats", () =>
        HttpResponse.json({ ...DEFAULT_JOB_STATS, last_claim_at: null }),
      ),
    );
    await renderApp("/app/northstar/jobs");
    expect(await screen.findByText("Worker")).toBeInTheDocument();
    expect(screen.getByText("Stalled")).toBeInTheDocument();
  });

  it("hides the Worker stalled badge when the claim is fresh", async () => {
    await renderApp("/app/northstar/jobs");
    await screen.findByText("Queue depth");
    expect(screen.queryByText("Worker")).not.toBeInTheDocument();
  });

  it("hides the Worker stalled badge when nothing is pending, even if stale", async () => {
    server.use(
      http.get("/api/orgs/:slug/jobs/stats", () =>
        HttpResponse.json({
          ...DEFAULT_JOB_STATS,
          by_status: { pending: 0, running: 0, succeeded: 20, dead_letter: 2, cancelled: 0 },
          last_claim_at: null,
        }),
      ),
    );
    await renderApp("/app/northstar/jobs");
    await screen.findByText("Queue depth");
    expect(screen.queryByText("Worker")).not.toBeInTheDocument();
  });

  it("hides replay/cancel controls without jobs.manage", async () => {
    server.use(
      http.get("/api/me", () =>
        HttpResponse.json({
          ...DEFAULT_ME,
          memberships: [
            {
              ...DEFAULT_ME.memberships[0],
              permissions: ["organization.read", "jobs.read"],
            },
          ],
        }),
      ),
    );
    await renderApp("/app/northstar/jobs");
    await screen.findByText("export.webhook");
    expect(screen.queryByRole("button", { name: "Replay" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });
});
