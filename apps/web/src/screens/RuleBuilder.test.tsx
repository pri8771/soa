import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_RULES_DRAFT, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/processes/purchase-orders/rules";

describe("Rule builder (CFG-012)", () => {
  it("renders the rule with its generated deterministic expression", async () => {
    await renderApp(PATH);
    expect(await screen.findByDisplayValue("high-value")).toBeInTheDocument();
    // Acceptance: the generated deterministic expression is visible.
    expect(screen.getByText("(total > 5000)")).toBeInTheDocument();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("editing the threshold marks unsaved changes and saves with If-Match", async () => {
    const user = userEvent.setup();
    let patched: { headers: Headers; body: unknown } | null = null;
    server.use(
      http.patch(
        "/api/orgs/northstar/processes/purchase-orders/rules/versions/:id",
        async ({ request }) => {
          patched = { headers: request.headers, body: await request.json() };
          return HttpResponse.json(DEFAULT_RULES_DRAFT);
        },
      ),
    );
    await renderApp(PATH);
    const value = await screen.findByDisplayValue("5000");
    await user.clear(value);
    await user.type(value, "100");
    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();
    // The expression preview tracks the edit deterministically.
    expect(screen.getByText("(total > 100)")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(patched).not.toBeNull());
    expect(patched!.headers.get("If-Match")).toBe(String(DEFAULT_RULES_DRAFT.version));
    const body = patched!.body as {
      definition: { rules: { condition: { right: { value: unknown } } }[] };
    };
    // Money fields coerce the typed text back to a number.
    expect(body.definition.rules[0].condition.right.value).toBe(100);
  });

  it("run test cases surfaces the server's validation verdict", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/orgs/northstar/processes/purchase-orders/rules/validate", () =>
        HttpResponse.json({
          valid: false,
          message: "rules[0].test_cases[0]: expected triggered=True, got False",
        }),
      ),
    );
    await renderApp(PATH);
    await screen.findByDisplayValue("high-value");
    await user.click(screen.getByRole("button", { name: "Run test cases" }));
    expect(await screen.findByText("Validation failed")).toBeInTheDocument();
    expect(screen.getByText(/expected triggered=True/)).toBeInTheDocument();
  });

  it("server rejection on save shows a banner without losing the edit", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch("/api/orgs/northstar/processes/purchase-orders/rules/versions/:id", () =>
        HttpResponse.json(
          { error: { message: "rules[0]: duplicate rule key 'high-value'" } },
          { status: 422 },
        ),
      ),
    );
    await renderApp(PATH);
    const key = await screen.findByDisplayValue("high-value");
    await user.clear(key);
    await user.type(key, "other-key");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText("The rules were not saved")).toBeInTheDocument();
    expect(screen.getByText(/duplicate rule key/)).toBeInTheDocument();
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(screen.getByDisplayValue("other-key")).toBeInTheDocument();
  });

  it("publish is disabled while there are unsaved changes", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    const publishButton = await screen.findByRole("button", { name: "Publish" });
    expect(publishButton).toBeEnabled();
    const value = screen.getByDisplayValue("5000");
    await user.clear(value);
    await user.type(value, "250");
    expect(publishButton).toBeDisabled();
  });

  it("adding a rule starts from a typed condition over schema fields", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByDisplayValue("high-value");
    await user.click(screen.getByRole("button", { name: "Add rule" }));
    // New rule defaults to a presence check on the first schema field.
    expect(await screen.findByText("is_present(po_number)")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Rule key")).toHaveLength(2);
  });
});
