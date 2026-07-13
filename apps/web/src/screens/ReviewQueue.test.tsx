import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_REVIEW_TASKS, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/review";
const OPEN_TASK = DEFAULT_REVIEW_TASKS[0];

describe("Review queue (REV-003)", () => {
  it("shows the queue with reason summaries, SLA, priority, and assignment", async () => {
    await renderApp(PATH);
    const table = await screen.findByRole("table", { name: "Review tasks" });
    expect(within(table).getByText("po-4713.pdf")).toBeInTheDocument();
    // Reason summary: first reason + count of the rest, source named.
    expect(
      within(table).getByText("rule triggered: totals.header_matches_lines +1 more"),
    ).toBeInTheDocument();
    expect(within(table).getByText("ambiguous reading: currency")).toBeInTheDocument();
    // SLA and blocking state are explicit.
    expect(within(table).getByText("overdue")).toBeInTheDocument();
    expect(within(table).getByText("blocked")).toBeInTheDocument();
    // Assignment: my task reads "me".
    expect(within(table).getByText("me")).toBeInTheDocument();
    // Actions match state: claim for open, release for mine.
    expect(within(table).getByRole("button", { name: "Claim po-4713.pdf" })).toBeInTheDocument();
    expect(within(table).getByRole("button", { name: "Release po-4711.pdf" })).toBeInTheDocument();
  });

  it("persists the view in the URL and narrows the list", async () => {
    const user = userEvent.setup();
    const { router } = await renderApp(PATH);
    await screen.findByText("po-4713.pdf");
    await user.click(screen.getByRole("button", { name: /View/ }));
    await user.click(await screen.findByRole("option", { name: "Mine" }));
    await waitFor(() => expect(screen.queryByText("po-4713.pdf")).not.toBeInTheDocument());
    expect(screen.getByText("po-4711.pdf")).toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ view: "mine" });
  });

  it("start next claims atomically and explains why that task is next", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByText("po-4713.pdf");
    await user.click(screen.getByRole("button", { name: "Start next" }));
    expect(await screen.findByText("Claimed po-4713.pdf")).toBeInTheDocument();
    expect(
      screen.getByText(/Highest-priority open task, oldest first within the same priority/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open in Review Studio" })).toBeInTheDocument();
  });

  it("says so when the queue is clear", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/orgs/northstar/review-tasks/claim-next", () =>
        HttpResponse.json({ task: null, explanation: "No open tasks to claim." }),
      ),
    );
    await renderApp(PATH);
    await screen.findByText("po-4713.pdf");
    await user.click(screen.getByRole("button", { name: "Start next" }));
    expect(await screen.findByText("Queue is clear")).toBeInTheDocument();
    expect(screen.getByText("No open tasks to claim.")).toBeInTheDocument();
  });

  it("surfaces a lost claim race instead of pretending", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/claim", () =>
        HttpResponse.json(
          { error: { message: "The task is in_progress, assigned to user:u-2." } },
          { status: 409 },
        ),
      ),
    );
    await renderApp(PATH);
    await screen.findByText("po-4713.pdf");
    await user.click(screen.getByRole("button", { name: "Claim po-4713.pdf" }));
    expect(await screen.findByText("Claim failed")).toBeInTheDocument();
    expect(screen.getByText(/assigned to user:u-2/)).toBeInTheDocument();
  });

  it("rows are keyboard-activatable deep links into the Review Studio", async () => {
    const user = userEvent.setup();
    const { router } = await renderApp(PATH);
    const table = await screen.findByRole("table", { name: "Review tasks" });
    const row = within(table).getByText("po-4713.pdf").closest("tr");
    expect(row).not.toBeNull();
    (row as HTMLElement).focus();
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(router.state.location.pathname).toBe(`/app/northstar/review/${OPEN_TASK.id}`),
    );
  });

  it("shows explicit empty and error states", async () => {
    server.use(
      http.get("/api/orgs/northstar/review-tasks", () =>
        HttpResponse.json({ items: [], has_more: false, next_cursor: null }),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText("No review tasks")).toBeInTheDocument();
  });

  it("shows the error state with retry when the queue fails to load", async () => {
    server.use(
      http.get("/api/orgs/northstar/review-tasks", () =>
        HttpResponse.json({ error: { message: "boom" } }, { status: 500 }),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText(/Couldn’t load the review queue/)).toBeInTheDocument();
  });
});
