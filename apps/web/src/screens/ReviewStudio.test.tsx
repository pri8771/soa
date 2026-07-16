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

  it("auto-locates a typed value and saves its region as evidence", async () => {
    const user = userEvent.setup();
    const posted: Record<string, unknown>[] = [];
    const box = [
      [130, 20],
      [260, 20],
      [260, 40],
      [130, 40],
    ];
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/locate", () =>
        HttpResponse.json({ found: true, page_number: 1, polygon: box }),
      ),
      http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", async ({ request }) => {
        posted.push((await request.json()) as Record<string, unknown>);
        return HttpResponse.json({
          correction: {
            id: "cor-1",
            field_key: "po_number",
            row_index: null,
            previous_raw_value: null,
            corrected_raw_value: "8077219",
            corrected_normalized_value: "8077219",
            normalization_error: null,
            corrected_by: "user:u-1",
          },
          task_version: 4,
          revalidation: null,
        });
      }),
    );
    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.clear(input);
    await user.type(input, "8077219{Enter}");
    await waitFor(() => expect(posted).toHaveLength(1));
    // The correction carries the located region so the viewer highlights it.
    expect(posted[0]).toMatchObject({
      field_key: "po_number",
      value: "8077219",
      evidence_selection: { page_number: 1, polygon: box, quote: "8077219" },
    });
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
    await user.click(input);
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

  it("redirects a superseded task URL to the document's current active task", async () => {
    // A reprocess cancels the old task and opens a fresh one; the stale URL
    // must land the reviewer on the live task, not on dead data.
    const nextTaskId = "c3333333-3333-4333-8333-333333333333";
    server.use(
      http.get("/api/orgs/northstar/review-tasks/:taskId/workspace", ({ params }) => {
        const id = String(params["taskId"]);
        const superseded = id !== nextTaskId;
        return HttpResponse.json({
          ...DEFAULT_WORKSPACE,
          task: { ...DEFAULT_WORKSPACE.task, id, state: superseded ? "cancelled" : "open" },
          superseded_by_task_id: superseded ? nextTaskId : null,
        });
      }),
    );
    const { router } = await renderApp(PATH);
    await waitFor(() =>
      expect(router.state.location.pathname).toBe(`/app/northstar/review/${nextTaskId}`),
    );
  });
});

describe("Review Studio required keyboard shortcuts", () => {
  it("uses J/K for review reasons, shows ? help, and never hijacks typing", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/orgs/northstar/review-tasks/:taskId/workspace", () =>
        HttpResponse.json({
          ...DEFAULT_WORKSPACE,
          task: {
            ...DEFAULT_WORKSPACE.task,
            reasons: [
              ...DEFAULT_WORKSPACE.task.reasons,
              {
                code: "low_confidence",
                message: "total needs a second look",
                field_key: "total_amount",
                row_index: null,
                rule_key: null,
              },
            ],
          },
        }),
      ),
    );
    await renderApp(PATH);
    await screen.findByRole("region", { name: "Header fields" });

    await user.keyboard("j");
    await waitFor(() => expect(screen.getByLabelText("po number")).toHaveFocus());
    await user.click(screen.getByText("Current decision:"));
    await user.keyboard("j");
    await waitFor(() => expect(screen.getByLabelText("total amount")).toHaveFocus());
    await user.click(screen.getByText("Current decision:"));
    await user.keyboard("k");
    await waitFor(() => expect(screen.getByLabelText("po number")).toHaveFocus());

    // Question mark and navigation letters remain ordinary text inside a
    // typing surface; no global action fires there.
    const search = screen.getByLabelText("Search text");
    await user.click(search);
    await user.keyboard("j?");
    expect(search).toHaveValue("j?");
    expect(
      screen.queryByRole("dialog", { name: "Review keyboard shortcuts" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByText("Current decision:"));
    await user.keyboard("?");
    expect(screen.getByRole("dialog", { name: "Review keyboard shortcuts" })).toBeInTheDocument();
  });

  it("E focuses evidence and M focuses matching for the active field", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);

    await user.click(await screen.findByLabelText("po number"));
    await screen.findByRole("button", { name: /Evidence for po_number/ });
    await user.click(screen.getByText("Current decision:"));
    await user.keyboard("e");
    expect(screen.getByRole("button", { name: /Evidence for po_number/ })).toHaveFocus();

    await user.click(screen.getByLabelText("sku row 0"));
    await screen.findByRole("region", { name: "Catalog match for SKU" });
    await user.click(screen.getByText("2 row(s)"));
    await user.keyboard("m");
    expect(screen.getByLabelText("Search catalog for SKU")).toHaveFocus();
  });

  it("C opens a working comment composer", async () => {
    const user = userEvent.setup();
    const posted: unknown[] = [];
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/comments", async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json(
          {
            id: "comment-1",
            task_id: DEFAULT_WORKSPACE.task.id,
            document_id: DEFAULT_WORKSPACE.document.id,
            author: "user:u-1",
            body: "Please verify this with @sam",
            mentions: ["sam"],
            created_at: "2026-07-12T10:05:00+00:00",
          },
          { status: 201 },
        );
      }),
    );
    await renderApp(PATH);
    await screen.findByRole("heading", { name: "Discussion" });

    await user.keyboard("c");
    const comment = screen.getByLabelText("Add comment");
    expect(comment).toHaveFocus();
    await user.type(comment, "Please verify this with @sam");
    await user.click(screen.getByRole("button", { name: "Add comment" }));
    await waitFor(() => expect(posted).toEqual([{ body: "Please verify this with @sam" }]));
    expect(await screen.findByText("Please verify this with @sam")).toBeInTheDocument();
  });

  it("A opens approval and R opens the available reject/escalate path", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByRole("button", { name: "Approve order…" });

    await user.keyboard("a");
    expect(screen.getByRole("button", { name: "Confirm approval" })).toBeInTheDocument();
    await user.keyboard("r");
    // The default reviewer cannot reject, so R falls back to escalation.
    expect(screen.getByLabelText(/Escalation reason/)).toBeInTheDocument();
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
  it("blocks the edit-blur approval race until the queued save completes", async () => {
    const user = userEvent.setup();
    let correctionPosts = 0;
    let approvalPosts = 0;
    let releaseSave: (() => void) | undefined;
    const saveGate = new Promise<void>((resolve) => {
      releaseSave = resolve;
    });
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        correctionPosts += 1;
        await saveGate;
        return HttpResponse.json({
          correction: {
            id: "cor-delayed",
            field_key: body["field_key"],
            row_index: body["row_index"] ?? null,
            previous_raw_value: "PO-1000A2",
            corrected_raw_value: body["value"],
            corrected_normalized_value: body["value"],
            normalization_error: null,
            corrected_by: "user:u-1",
          },
          task_version: 4,
          revalidation: null,
        });
      }),
      http.post("/api/orgs/northstar/review-tasks/:taskId/approve", () => {
        approvalPosts += 1;
        return HttpResponse.json({});
      }),
    );

    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.clear(input);
    await user.type(input, "PO-DELAYED");

    // Pointer-down moves focus away from the field. Its blur queues the
    // save before React Aria can dispatch the button press.
    const approveButton = screen.getByRole("button", { name: "Approve order…" });
    await user.click(approveButton);
    await waitFor(() => expect(correctionPosts).toBe(1));
    expect(approvalPosts).toBe(0);
    expect(approveButton).toBeDisabled();
    expect(screen.getByText(/queued or saving change/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm approval" })).not.toBeInTheDocument();

    releaseSave?.();
    await waitFor(() => expect(approveButton).toBeEnabled());
  });

  it("keeps approval disabled after a field save fails", async () => {
    const user = userEvent.setup();
    let approvalPosts = 0;
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", () =>
        HttpResponse.json({ error: { message: "temporary correction outage" } }, { status: 503 }),
      ),
      http.post("/api/orgs/northstar/review-tasks/:taskId/approve", () => {
        approvalPosts += 1;
        return HttpResponse.json({});
      }),
    );

    await renderApp(PATH);
    const input = await screen.findByLabelText("po number");
    await user.clear(input);
    await user.type(input, "PO-FAILED{Enter}");
    expect(await screen.findByText(/Not saved: temporary correction outage/)).toBeInTheDocument();
    const approveButton = screen.getByRole("button", { name: "Approve order…" });
    expect(approveButton).toBeDisabled();
    expect(screen.getByText(/failed to save.*Retry/i)).toBeInTheDocument();
    expect(approvalPosts).toBe(0);
  });

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

  it("approving invalidates the queues and dashboard the task appears on", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/approve", () =>
        HttpResponse.json({
          status: "approved",
          idempotent: false,
          task_version: 5,
          warnings: [],
          override_used: false,
          task: { ...DEFAULT_WORKSPACE.task, state: "completed", outcome: "approved" },
        }),
      ),
    );
    const { queryClient } = await renderApp(PATH);
    // Seed cached queue/dashboard entries as if the user had visited them.
    const seeded = [
      ["review-tasks", "northstar", "all", "priority"],
      ["documents", "northstar", "all", "all", ""],
      ["operations", "northstar"],
    ] as const;
    for (const key of seeded) queryClient.setQueryData(key, {});

    await user.click(await screen.findByRole("button", { name: "Approve order…" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    await screen.findByText("Order approved.");
    for (const key of seeded) {
      expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true);
    }
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

describe("Review Studio catalog matching (CAT-010)", () => {
  it("offers the picker on a catalog-matched cell and posts the best-match pick", async () => {
    const user = userEvent.setup();
    const posted: Record<string, unknown>[] = [];
    server.use(
      http.post(
        "/api/orgs/northstar/review-tasks/:taskId/catalog-selection",
        async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>;
          posted.push(body);
          return HttpResponse.json({
            correction: {
              id: "cor-cat-1",
              field_key: body["field_key"],
              row_index: body["row_index"],
              previous_raw_value: "WID-1OO",
              corrected_raw_value: body["selected_source_id"],
              corrected_normalized_value: body["selected_source_id"],
              normalization_error: null,
              corrected_by: "user:u-1",
            },
            task_version: 4,
            override: false,
            revalidation: {
              evaluation: { blocking: false },
              decision: { route: "approved", reasons: [] },
            },
          });
        },
      ),
    );
    await renderApp(PATH);

    // A field the catalog does not match offers no picker.
    await user.click(await screen.findByLabelText("po number"));
    expect(screen.queryByRole("region", { name: /Catalog match/ })).not.toBeInTheDocument();

    // Focusing a sku cell offers the picker, seeded with the cell value.
    await user.click(await screen.findByLabelText("sku row 0"));
    const picker = await screen.findByRole("region", { name: "Catalog match for SKU" });
    const search = within(picker).getByLabelText("Search catalog for SKU");
    expect(search).toHaveValue("WID-100");

    // Search: ranked candidates with score and per-feature explanation.
    await user.click(within(picker).getByRole("button", { name: "Search catalog" }));
    expect(await within(picker).findByText("WID-100")).toBeInTheDocument();
    expect(within(picker).getByText("82% match")).toBeInTheDocument();
    expect(within(picker).getByText("best match")).toBeInTheDocument();
    const [explain] = within(picker).getAllByRole("button", { name: "Why this score" });
    await user.click(explain);
    expect(within(picker).getByText(/trigram overlap with 'Widget 100'/)).toBeInTheDocument();

    // Picking the best match posts the selection without a reason.
    await user.click(within(picker).getByRole("button", { name: "Pick WID-100" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      field_key: "lines.sku",
      row_index: 0,
      query: "WID-100",
      selected_source_id: "WID-100",
      expected_version: 3,
    });
    expect(posted[0]).not.toHaveProperty("reason");
    // The outcome is announced and the fresh decision is on screen.
    expect(await screen.findByText(/Matched: the field is now “WID-100”/)).toBeInTheDocument();
    expect(await screen.findByText("approved")).toBeInTheDocument();
  });

  it("picking a non-best candidate demands a written override reason", async () => {
    const user = userEvent.setup();
    const posted: Record<string, unknown>[] = [];
    server.use(
      http.post(
        "/api/orgs/northstar/review-tasks/:taskId/catalog-selection",
        async ({ request }) => {
          posted.push((await request.json()) as Record<string, unknown>);
          return HttpResponse.json({
            correction: {
              id: "cor-cat-2",
              field_key: "lines.sku",
              row_index: 0,
              previous_raw_value: "WID-1OO",
              corrected_raw_value: "GAD-205",
              corrected_normalized_value: "GAD-205",
              normalization_error: null,
              corrected_by: "user:u-1",
            },
            task_version: 4,
            override: true,
            revalidation: null,
          });
        },
      ),
    );
    await renderApp(PATH);
    await user.click(await screen.findByLabelText("sku row 0"));
    const picker = await screen.findByRole("region", { name: "Catalog match for SKU" });
    await user.click(within(picker).getByRole("button", { name: "Search catalog" }));
    await user.click(await within(picker).findByRole("button", { name: "Pick GAD-205" }));

    // Nothing posted yet — the override needs a stated reason first.
    const override = within(picker).getByRole("group", { name: "Manual override" });
    expect(posted).toHaveLength(0);
    await user.type(
      within(override).getByLabelText(/Override reason/),
      "buyer's code maps to the gadget",
    );
    await user.click(within(override).getByRole("button", { name: "Pick with override" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      selected_source_id: "GAD-205",
      reason: "buyer's code maps to the gadget",
    });
    expect(await screen.findByText(/manual override recorded/)).toBeInTheDocument();
  });

  it("a stream without a usable catalog is stated in the picker, not a crash", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/orgs/northstar/review-tasks/:taskId/catalog-candidates", () =>
        HttpResponse.json({
          available: false,
          reason: "The document's stream has no products catalog bound.",
          candidates: [],
        }),
      ),
    );
    await renderApp(PATH);
    await user.click(await screen.findByLabelText("sku row 0"));
    const picker = await screen.findByRole("region", { name: "Catalog match for SKU" });
    await user.click(within(picker).getByRole("button", { name: "Search catalog" }));
    expect(await within(picker).findByText("Catalog lookup failed")).toBeInTheDocument();
    expect(
      within(picker).getByText("The document's stream has no products catalog bound."),
    ).toBeInTheDocument();
  });
});
