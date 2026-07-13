import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_SCHEMA_DRAFT, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/processes/purchase-orders/schema";

describe("Schema builder (CFG-011)", () => {
  it("renders the draft field tree and preview", async () => {
    await renderApp(PATH);
    expect(await screen.findByDisplayValue("po_number")).toBeInTheDocument();
    expect(screen.getByDisplayValue("total")).toBeInTheDocument();
    // Preview mirrors the definition.
    expect(screen.getByLabelText("Schema preview")).toHaveTextContent('"po_number"');
    // No unsaved changes on load.
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("editing marks unsaved changes and saves with If-Match", async () => {
    const user = userEvent.setup();
    let patched: { headers: Headers; body: unknown } | null = null;
    server.use(
      http.patch(
        "/api/orgs/northstar/processes/purchase-orders/schema/versions/:id",
        async ({ request }) => {
          patched = { headers: request.headers, body: await request.json() };
          return HttpResponse.json(DEFAULT_SCHEMA_DRAFT);
        },
      ),
    );
    await renderApp(PATH);
    const keyInput = await screen.findByDisplayValue("total");
    await user.clear(keyInput);
    await user.type(keyInput, "grand_total");
    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(patched).not.toBeNull());
    expect(patched!.headers.get("If-Match")).toBe(String(DEFAULT_SCHEMA_DRAFT.version));
    const body = patched!.body as { definition: { fields: { key: string }[] } };
    expect(body.definition.fields.map((f) => f.key)).toContain("grand_total");
  });

  it("keyboard reorder moves a field and updates the preview order", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByDisplayValue("po_number");
    await user.click(screen.getByRole("button", { name: "Move total up" }));
    const preview = screen.getByLabelText("Schema preview").textContent ?? "";
    expect(preview.indexOf('"total"')).toBeLessThan(preview.indexOf('"po_number"'));
  });

  it("server validation errors surface without losing the edit", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch("/api/orgs/northstar/processes/purchase-orders/schema/versions/:id", () =>
        HttpResponse.json({ error: { message: "duplicate field key 'total'" } }, { status: 422 }),
      ),
    );
    await renderApp(PATH);
    const keyInput = await screen.findByDisplayValue("total");
    await user.clear(keyInput);
    await user.type(keyInput, "po_number");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText("The schema was not saved")).toBeInTheDocument();
    expect(screen.getByText(/duplicate field key/)).toBeInTheDocument();
    // The unsaved edit is still there for the user to fix.
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
  });

  it("publish is disabled while there are unsaved changes", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    const publishButton = await screen.findByRole("button", { name: "Publish" });
    expect(publishButton).toBeEnabled();
    const keyInput = screen.getByDisplayValue("total");
    await user.clear(keyInput);
    await user.type(keyInput, "amount");
    expect(publishButton).toBeDisabled();
  });

  it("adding a table field exposes column editing", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByDisplayValue("po_number");
    await user.click(screen.getByRole("button", { name: "Add field" }));
    // New empty field appears; switch it to a table type.
    const typeSelects = screen.getAllByRole("button", { name: /Type/ });
    await user.click(typeSelects[typeSelects.length - 1]);
    await user.click(await screen.findByRole("option", { name: "table" }));
    expect(await screen.findByRole("button", { name: "Add column" })).toBeInTheDocument();
  });
});
