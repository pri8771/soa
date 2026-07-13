import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_INTEGRATION, DEFAULT_MAPPING_DRAFT, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/integrations/erp";

describe("Mapping studio (EXP-004)", () => {
  it("shows the version state, mapping rows, and required-field status", async () => {
    await renderApp(PATH);
    expect(
      await screen.findByRole("heading", { name: "Mapping: Northstar ERP webhook" }),
    ).toBeInTheDocument();
    // Version state is visible.
    expect(screen.getByText("v1 — draft")).toBeInTheDocument();

    // Rows render from the definition.
    const row1 = screen.getByRole("listitem", { name: "Mapping row 1" });
    expect(within(row1).getByLabelText("Target field")).toHaveValue("PoNumber");
    expect(within(row1).getByLabelText("Source path")).toHaveValue("identifiers.po_number");
    const row2 = screen.getByRole("listitem", { name: "Mapping row 2" });
    expect(within(row2).getByLabelText("Transform")).toHaveValue("date");
    expect(within(row2).getByLabelText(/Pattern/)).toHaveValue("MM/DD/YYYY");

    // Required-field status: PoNumber is produced, GrandTotal is not.
    expect(screen.getByText("PoNumber: mapped")).toBeInTheDocument();
    expect(screen.getByText("GrandTotal: NOT mapped")).toBeInTheDocument();
  });

  it("saves with If-Match after edits and gates validate/publish on a clean draft", async () => {
    const user = userEvent.setup();
    const patched: { headers: Headers; body: Record<string, unknown> }[] = [];
    server.use(
      http.patch(
        "/api/orgs/northstar/integrations/erp/mapping-versions/:versionId",
        async ({ request }) => {
          patched.push({
            headers: request.headers,
            body: (await request.json()) as Record<string, unknown>,
          });
          return HttpResponse.json({ ...DEFAULT_MAPPING_DRAFT, version: 4 });
        },
      ),
    );
    await renderApp(PATH);
    const row1 = await screen.findByRole("listitem", { name: "Mapping row 1" });
    const target = within(row1).getByLabelText("Target field");
    await user.clear(target);
    await user.type(target, "PurchaseOrder");

    expect(screen.getByText("unsaved changes")).toBeInTheDocument();
    // Validation/publish wait for a saved draft.
    expect(screen.getByRole("button", { name: "Validate with sample" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(patched).toHaveLength(1));
    expect(patched[0].headers.get("If-Match")).toBe(String(DEFAULT_MAPPING_DRAFT.version));
    const definition = patched[0].body["definition"] as {
      fields: { target: string }[];
    };
    expect(definition.fields[0].target).toBe("PurchaseOrder");
    expect(await screen.findByText("Draft saved.")).toBeInTheDocument();
  });

  it("links validation errors to their mapping rows", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/orgs/northstar/integrations/erp/mapping-versions/:versionId/validate", () =>
        HttpResponse.json({
          valid: false,
          errors: [
            "$.fields[1]: unknown format {'kind': 'python_eval'}",
            "$.lines.fields[0]: 'source' must be a non-empty dot path",
            "target-schema: GrandTotal is required",
          ],
          payload: null,
          trace: [],
        }),
      ),
    );
    await renderApp(PATH);
    await screen.findByText("v1 — draft");
    await user.click(screen.getByRole("button", { name: "Validate with sample" }));
    await screen.findByText("Validation found problems.");

    // The header row 2 error is pinned to row 2.
    const row2 = screen.getByRole("listitem", { name: "Mapping row 2" });
    expect(row2).toHaveAttribute("aria-invalid", "true");
    expect(within(row2).getByRole("alert")).toHaveTextContent("python_eval");
    // The line row error is pinned to the line row.
    const lineRow = screen.getByRole("listitem", { name: "Line mapping row 1" });
    expect(lineRow).toHaveAttribute("aria-invalid", "true");
    // The unpinnable error surfaces as a general banner.
    expect(screen.getByText(/GrandTotal is required/)).toBeInTheDocument();
    // Untouched rows are not blamed.
    expect(screen.getByRole("listitem", { name: "Mapping row 1" })).toHaveAttribute(
      "aria-invalid",
      "false",
    );
  });

  it("shows the sample preview when validation passes", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByText("v1 — draft");
    await user.click(screen.getByRole("button", { name: "Validate with sample" }));
    expect(await screen.findByText("Mapping is valid — preview below.")).toBeInTheDocument();
    expect(screen.getByTestId("mapping-preview")).toHaveTextContent('"OrderDate": "03/14/2026"');
  });

  it("published versions are read-only with a new-draft path", async () => {
    server.use(
      http.get("/api/orgs/northstar/integrations/:integrationSlug", () =>
        HttpResponse.json({
          integration: DEFAULT_INTEGRATION,
          mapping_versions: [
            { ...DEFAULT_MAPPING_DRAFT, state: "published", published_by: "user:u-1" },
          ],
        }),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText("v1 — published")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save draft" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New draft" })).toBeInTheDocument();
    const row1 = screen.getByRole("listitem", { name: "Mapping row 1" });
    expect(within(row1).getByLabelText("Target field")).toHaveAttribute("readonly");
  });
});
