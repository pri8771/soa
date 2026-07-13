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
    expect(screen.getByRole("button", { name: "Reload the workspace" })).toBeInTheDocument();
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
    expect(screen.getByText(/assigned to user:u-2/)).toBeInTheDocument();
    expect(screen.getByLabelText("po number")).toHaveAttribute("readonly");
  });
});
