import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_STREAM_DRAFT, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/streams/email/configure";

describe("Stream inheritance editor (CFG-013)", () => {
  it("labels every setting with its provenance and shows the resolved preview", async () => {
    await renderApp(PATH);
    // Draft override.
    expect(await screen.findByDisplayValue("0.9")).toBeInTheDocument();
    expect((await screen.findAllByText("Overridden on this stream")).length).toBeGreaterThan(0);
    // Process-inherited and platform-default settings are labeled too.
    expect(screen.getByDisplayValue("de")).toBeInTheDocument();
    expect(screen.getAllByText("Inherited from process").length).toBeGreaterThan(0);
    expect(screen.getByDisplayValue("50")).toBeInTheDocument();
    expect(screen.getAllByText("Platform default").length).toBeGreaterThan(0);
    // Affected scope is spelled out.
    expect(screen.getByText("Scope of these changes")).toBeInTheDocument();
    expect(screen.getByText(/affect only the “Email intake” stream/)).toBeInTheDocument();
    // Resolved preview carries the fingerprint.
    expect(await screen.findByText(/fingerprint/)).toBeInTheDocument();
  });

  it("reset-to-parent removes the override and saves with If-Match", async () => {
    const user = userEvent.setup();
    let patched: { headers: Headers; body: unknown } | null = null;
    server.use(
      http.patch("/api/orgs/northstar/streams/email/versions/:id", async ({ request }) => {
        patched = { headers: request.headers, body: await request.json() };
        return HttpResponse.json(DEFAULT_STREAM_DRAFT);
      }),
    );
    await renderApp(PATH);
    await screen.findByDisplayValue("0.9");
    await user.click(screen.getByRole("button", { name: "Reset confidence_floor to parent" }));
    // The inherited value shows through immediately.
    expect(await screen.findByDisplayValue("0.85")).toBeInTheDocument();
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(patched).not.toBeNull());
    expect(patched!.headers.get("If-Match")).toBe(String(DEFAULT_STREAM_DRAFT.version));
    expect((patched!.body as { overrides: unknown }).overrides).toEqual({});
  });

  it("editing an inherited value creates a typed override", async () => {
    const user = userEvent.setup();
    let patched: { body: unknown } | null = null;
    server.use(
      http.patch("/api/orgs/northstar/streams/email/versions/:id", async ({ request }) => {
        patched = { body: await request.json() };
        return HttpResponse.json(DEFAULT_STREAM_DRAFT);
      }),
    );
    await renderApp(PATH);
    const maxPages = await screen.findByLabelText("max_pages");
    await user.clear(maxPages);
    await user.type(maxPages, "10");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(patched).not.toBeNull());
    const overrides = (patched!.body as { overrides: Record<string, unknown> }).overrides;
    // Numbers stay numbers, and the existing override is preserved.
    expect(overrides["max_pages"]).toBe(10);
    expect(overrides["confidence_floor"]).toBe(0.9);
  });

  it("a new setting is 'not configured' until added, then overridden", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByDisplayValue("0.9");
    await user.type(screen.getByLabelText("Setting key"), "retention_days");
    expect(screen.getByText("Not configured")).toBeInTheDocument();
    await user.type(screen.getByLabelText("Setting value"), "30");
    await user.click(screen.getByRole("button", { name: "Add override" }));
    // The new setting shows as an override with its typed value.
    expect(await screen.findByLabelText("retention_days")).toHaveValue("30");
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(screen.queryByText("Not configured")).not.toBeInTheDocument();
  });

  it("editing still works when no resolved preview is available", async () => {
    server.use(
      http.post("/api/orgs/northstar/streams/email/resolve", () =>
        HttpResponse.json(
          { error: { message: "The parent process has no published version to resolve against." } },
          { status: 409 },
        ),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText("Resolved preview unavailable")).toBeInTheDocument();
    // The draft's own overrides remain editable.
    expect(screen.getByDisplayValue("0.9")).toBeInTheDocument();
  });

  it("edits and publishes extraction instructions attached to the stream version", async () => {
    const user = userEvent.setup();
    let updated: Record<string, unknown> | null = null;
    let published = false;
    server.use(
      http.patch("/api/orgs/northstar/instructions/:instructionId", async ({ request }) => {
        updated = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          id: "instruction-1",
          stream_version_id: DEFAULT_STREAM_DRAFT.id,
          schema_version_id: "50000000-0000-4000-8000-000000000001",
          version_number: 1,
          state: "draft",
          reference: "instruction:instruction-1:v1",
          content: updated["content"],
          change_summary: updated["change_summary"],
          published_at: null,
          published_by: null,
        });
      }),
      http.post("/api/orgs/northstar/instructions/:instructionId/publish", () => {
        published = true;
        return HttpResponse.json({
          id: "instruction-1",
          stream_version_id: DEFAULT_STREAM_DRAFT.id,
          schema_version_id: "50000000-0000-4000-8000-000000000001",
          version_number: 1,
          state: "published",
          reference: "instruction:instruction-1:v1",
          content: {
            instructions: "Extract identifiers, totals, and requested delivery date.",
            field_guidance: { po_number: "Preserve leading zeroes." },
          },
          change_summary: "Cover requested delivery date",
          published_at: "2026-07-15T14:00:00Z",
          published_by: "user:test",
        });
      }),
    );

    await renderApp(PATH);
    const editor = await screen.findByRole("region", { name: "Extraction instructions" });
    const instructionText = await within(editor).findByDisplayValue(
      "Extract purchase-order identifiers and totals.",
    );
    await user.clear(instructionText);
    await user.type(instructionText, "Extract identifiers, totals, and requested delivery date.");
    const summary = within(editor).getByLabelText("Change summary");
    await user.clear(summary);
    await user.type(summary, "Cover requested delivery date");
    await user.click(within(editor).getByRole("button", { name: "Save instructions" }));

    await waitFor(() => expect(updated).not.toBeNull());
    expect(updated).toMatchObject({
      content: {
        instructions: "Extract identifiers, totals, and requested delivery date.",
        field_guidance: { po_number: "Preserve leading zeros." },
      },
      change_summary: "Cover requested delivery date",
    });

    await user.click(within(editor).getByRole("button", { name: "Publish instructions" }));
    expect(
      await screen.findByRole("dialog", { name: "Publish extraction instructions?" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Publish immutable version" }));
    await waitFor(() => expect(published).toBe(true));
  });

  it("allows an operator to author the first instruction before creating its draft", async () => {
    const user = userEvent.setup();
    let created: Record<string, unknown> | null = null;
    server.use(
      http.get("/api/orgs/northstar/stream-versions/:streamVersionId/instructions", () =>
        HttpResponse.json({ items: [] }),
      ),
      http.post(
        "/api/orgs/northstar/stream-versions/:streamVersionId/instructions",
        async ({ request, params }) => {
          created = (await request.json()) as Record<string, unknown>;
          return HttpResponse.json(
            {
              id: "instruction-first",
              stream_version_id: String(params["streamVersionId"]),
              schema_version_id: created["schema_version_id"],
              version_number: 1,
              state: "draft",
              reference: "instruction:instruction-first:v1",
              content: created["content"],
              change_summary: created["change_summary"],
              published_at: null,
              published_by: null,
            },
            { status: 201 },
          );
        },
      ),
    );

    await renderApp(PATH);
    const editor = await screen.findByRole("region", { name: "Extraction instructions" });
    const instructionText = within(editor).getByLabelText("Instruction text");
    expect(instructionText).not.toHaveAttribute("readonly");
    await user.type(instructionText, "Extract the exact purchase order number.");
    await user.click(within(editor).getByRole("button", { name: "Create instruction draft" }));

    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({
      schema_version_id: "50000000-0000-4000-8000-000000000001",
      content: { instructions: "Extract the exact purchase order number.", field_guidance: {} },
    });
  });
});
