import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_WORKSPACE, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = `/app/northstar/review/${DEFAULT_WORKSPACE.task.id}`;

describe("Review Studio header field editor (REV-007)", () => {
  it("shows raw/canonical values, confidence, validation, provenance, and candidates", async () => {
    await renderApp(PATH);
    const editor = await screen.findByRole("region", { name: "Header fields" });
    // The editor documents its keyboard map on itself.
    expect(editor).toHaveAttribute("aria-description", expect.stringContaining("Enter saves"));

    const po = within(editor).getByLabelText("po number").closest("li") as HTMLElement;
    expect(within(po).getByText("raw: PO-1000A2")).toBeInTheDocument();
    expect(within(po).getByText(/confidence 60% · mock@mock-v1/)).toBeInTheDocument();
    expect(within(po).getByText("review")).toBeInTheDocument();
    expect(within(po).getByText("needs attention")).toBeInTheDocument();
    expect(within(po).getByRole("button", { name: "PO-100042 (55%)" })).toBeInTheDocument();

    const total = within(editor).getByLabelText("total amount").closest("li") as HTMLElement;
    expect(within(total).getByText("canonical: 1234.50 USD")).toBeInTheDocument();
    expect(within(total).getByText("passed")).toBeInTheDocument();
  });

  it("autosaves on Enter with the task version, shows the save state, and updates the decision", async () => {
    const user = userEvent.setup();
    const posted: unknown[] = [];
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        posted.push(body);
        return HttpResponse.json({
          correction: {
            id: "cor-1",
            field_key: body["field_key"],
            row_index: null,
            previous_raw_value: "PO-1000A2",
            corrected_raw_value: body["value"],
            corrected_normalized_value: body["value"],
            normalization_error: null,
            corrected_by: "user:u-1",
          },
          task_version: 4 + posted.length,
          revalidation: {
            evaluation: { blocking: false },
            decision: { route: "approved", reasons: [] },
          },
        });
      }),
    );
    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.clear(input);
    await user.type(input, "PO-100042{Enter}");
    await waitFor(() => expect(screen.getByText("Saved")).toBeInTheDocument());
    expect(posted[0]).toMatchObject({
      field_key: "po_number",
      value: "PO-100042",
      expected_version: 3, // the workspace's task version
    });
    // The fresh decision from revalidation is on screen.
    expect(await screen.findByText("approved")).toBeInTheDocument();
    expect(screen.getByText("no open reasons")).toBeInTheDocument();

    // A second save is authored against the ADVANCED version.
    await user.clear(input);
    await user.type(input, "PO-7{Enter}");
    await waitFor(() => expect(posted).toHaveLength(2));
    expect(posted[1]).toMatchObject({ expected_version: 5 });
  });

  it("adopting a candidate saves it as the corrected value", async () => {
    const user = userEvent.setup();
    const posted: unknown[] = [];
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json({
          correction: {
            id: "cor-1",
            field_key: "po_number",
            row_index: null,
            previous_raw_value: "PO-1000A2",
            corrected_raw_value: "PO-100042",
            corrected_normalized_value: "PO-100042",
            normalization_error: null,
            corrected_by: "user:u-1",
          },
          task_version: 4,
          revalidation: null,
        });
      }),
    );
    await renderApp(PATH);
    await user.click(await screen.findByRole("button", { name: "PO-100042 (55%)" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ field_key: "po_number", value: "PO-100042" });
  });

  it("a stale-version conflict surfaces loudly with a reload action", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", () =>
        HttpResponse.json(
          {
            error: {
              message:
                "The task changed since you loaded it (server version 9, yours 3). Reload before editing — nothing was saved.",
            },
          },
          { status: 409 },
        ),
      ),
    );
    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.clear(input);
    await user.type(input, "PO-X{Enter}");
    expect(await screen.findByText("Someone else changed this task")).toBeInTheDocument();
    expect(screen.getAllByText(/nothing was saved/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /Reload the workspace/ })).toBeInTheDocument();
    // The field-level state says it too, for screen readers.
    expect(screen.getByText(/Not saved:/)).toBeInTheDocument();
  });

  it("the reason navigator focuses the field the reason names", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByRole("region", { name: "Header fields" });
    await user.click(screen.getByRole("button", { name: /Go to reason 1 of 1/ }));
    expect(screen.getByLabelText("po number")).toHaveFocus();
    expect(
      screen.getByText("confidence 0.60 is below the critical gate of 0.98"),
    ).toBeInTheDocument();
  });

  it("focusing a field highlights its source in the viewer (field -> source)", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.click(input);
    // The viewer draws the evidence overlay for the focused field.
    const overlay = await screen.findByRole("button", { name: /Evidence for po_number/ });
    expect(overlay).toBeInTheDocument();
  });

  it("Alt+ArrowDown moves to the next field (documented keyboard map)", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    input.focus();
    await user.keyboard("{Alt>}{ArrowDown}{/Alt}");
    expect(screen.getByLabelText("total amount")).toHaveFocus();
    await user.keyboard("{Alt>}{ArrowUp}{/Alt}");
    expect(screen.getByLabelText("po number")).toHaveFocus();
  });

  it("is read-only when the task is not mine", async () => {
    server.use(
      http.get("/api/orgs/northstar/review-tasks/:taskId/workspace", () =>
        HttpResponse.json({
          ...DEFAULT_WORKSPACE,
          task: { ...DEFAULT_WORKSPACE.task, assigned_to: "user:u-2" },
        }),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText("Read-only")).toBeInTheDocument();
    // The banner and the approval panel both say why (disabled reason).
    expect(screen.getAllByText(/assigned to user:u-2/).length).toBeGreaterThan(0);
    expect(screen.getByLabelText("po number")).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Approve order…" })).toBeDisabled();
  });
});

describe("Review Studio conflict resolver (REV-014)", () => {
  /** First save is refused (409); the refreshed workspace carries the
   * rival editor's correction and the fresh task version 9. */
  function conflictScenario() {
    const posted: Record<string, unknown>[] = [];
    server.use(
      http.get("/api/orgs/northstar/review-tasks/:taskId/workspace", () =>
        HttpResponse.json({
          ...DEFAULT_WORKSPACE,
          task: { ...DEFAULT_WORKSPACE.task, version: 9 },
          corrections: [
            {
              field_key: "po_number",
              row_index: null,
              corrected_raw_value: "PO-9",
              corrected_normalized_value: "PO-9",
              normalization_error: null,
              corrected_by: "user:u-9",
            },
          ],
        }),
      ),
      http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        posted.push(body);
        if (posted.length === 1) {
          return HttpResponse.json(
            {
              error: {
                message:
                  "The task changed since you loaded it (server version 9, yours 3). " +
                  "Reload before editing — nothing was saved.",
              },
            },
            { status: 409 },
          );
        }
        return HttpResponse.json({
          correction: {
            id: "cor-2",
            field_key: body["field_key"],
            row_index: null,
            previous_raw_value: "PO-9",
            corrected_raw_value: body["value"],
            corrected_normalized_value: body["value"],
            normalization_error: null,
            corrected_by: "user:u-1",
          },
          task_version: 10,
          revalidation: null,
        });
      }),
    );
    return posted;
  }

  it("shows both values with editor metadata and the task state; keep-mine merges", async () => {
    const user = userEvent.setup();
    const posted = conflictScenario();
    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.clear(input);
    await user.type(input, "PO-X{Enter}");

    // Both values side by side, with who authored the server's.
    expect(await screen.findByText(/yours: “PO-X”/)).toBeInTheDocument();
    expect(screen.getByText(/server: “PO-9”/)).toBeInTheDocument();
    expect(screen.getByText(/corrected by user:u-9/)).toBeInTheDocument();
    // The task state stays unambiguous.
    expect(screen.getByText(/in progress — assigned to you \(version 9\)/)).toBeInTheDocument();

    // Keep mine: the held value is re-saved against the FRESH version.
    await user.click(screen.getByRole("button", { name: "Keep mine: po_number" }));
    await waitFor(() => expect(posted).toHaveLength(2));
    expect(posted[1]).toMatchObject({
      field_key: "po_number",
      value: "PO-X",
      expected_version: 9,
    });
    // The conflict is resolved and the banner is gone.
    await waitFor(() =>
      expect(screen.queryByText("Someone else changed this task")).not.toBeInTheDocument(),
    );
  });

  it("take-server adopts the rival value without posting anything", async () => {
    const user = userEvent.setup();
    const posted = conflictScenario();
    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.clear(input);
    await user.type(input, "PO-X{Enter}");
    await screen.findByText(/yours: “PO-X”/);

    await user.click(screen.getByRole("button", { name: "Use server value: po_number" }));
    // No second save happened; the editor now shows the server's value.
    expect(posted).toHaveLength(1);
    expect(screen.getByLabelText("po number")).toHaveValue("PO-9");
    expect(screen.queryByText("Someone else changed this task")).not.toBeInTheDocument();
  });

  it("reload-all is explicit about discarding the held edits", async () => {
    const user = userEvent.setup();
    conflictScenario();
    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.clear(input);
    await user.type(input, "PO-X{Enter}");
    await screen.findByText(/yours: “PO-X”/);
    const reload = screen.getByRole("button", { name: /Reload the workspace/ });
    expect(reload).toHaveAccessibleName(/discards the edits above/);
    await user.click(reload);
    await waitFor(() =>
      expect(screen.queryByText("Someone else changed this task")).not.toBeInTheDocument(),
    );
  });
});

describe("Review Studio approval actions (REV-013)", () => {
  it("approves via the two-step confirm and announces the outcome", async () => {
    const user = userEvent.setup();
    const posted: unknown[] = [];
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/approve", async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json({
          status: "approved",
          idempotent: false,
          task_version: 5,
          warnings: [],
          override_used: false,
          task: { ...DEFAULT_WORKSPACE.task, state: "completed", outcome: "approved" },
        });
      }),
    );
    await renderApp(PATH);
    // Step one only opens the completion summary.
    await user.click(await screen.findByRole("button", { name: "Approve order…" }));
    expect(posted).toHaveLength(0);
    expect(screen.getByText(/finding\(s\) remain/)).toBeInTheDocument();
    // Step two performs the approval and the outcome is announced.
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({});
    expect(await screen.findByText("Order approved.")).toBeInTheDocument();
  });

  it("escalates with a reason", async () => {
    const user = userEvent.setup();
    const posted: unknown[] = [];
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/escalate", async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json({
          ...DEFAULT_WORKSPACE.task,
          state: "open",
          assigned_to: null,
          priority: 10,
        });
      }),
    );
    await renderApp(PATH);
    await user.click(await screen.findByRole("button", { name: "Escalate…" }));
    await user.type(screen.getByLabelText(/Escalation reason/), "handwriting needs a supervisor");
    await user.click(screen.getByRole("button", { name: "Confirm escalation" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({ reason: "handwriting needs a supervisor" });
    expect(
      await screen.findByText("Task escalated and returned to the queue at top priority."),
    ).toBeInTheDocument();
  });

  it("the reviewer cannot reject (permission-gated) and is told why", async () => {
    await renderApp(PATH);
    expect(await screen.findByRole("button", { name: "Reject…" })).toBeDisabled();
    expect(screen.getByText(/documents.reject permission/)).toBeInTheDocument();
  });

  it("a completed task shows the outcome instead of actions", async () => {
    server.use(
      http.get("/api/orgs/northstar/review-tasks/:taskId/workspace", () =>
        HttpResponse.json({
          ...DEFAULT_WORKSPACE,
          task: {
            ...DEFAULT_WORKSPACE.task,
            state: "completed",
            outcome: "approved",
            assigned_to: null,
          },
        }),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText("This task is approved")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve order…" })).not.toBeInTheDocument();
  });
});
