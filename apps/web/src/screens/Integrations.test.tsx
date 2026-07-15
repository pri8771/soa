import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

describe("integration administration", () => {
  it("creates a supported integration without collecting a secret", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/integrations", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(
          {
            id: "integration-new",
            ...(body as object),
            status: "paused",
            credential_configured: false,
            production_ready: false,
            readiness_detail: "A vendor-specific idempotent upsert contract is still required.",
            idempotency_mechanism: "Not implemented; delivery is disabled.",
            active_mapping_version_id: null,
            version: 1,
            created_at: "2026-07-15T12:00:00Z",
          },
          { status: 201 },
        );
      }),
    );
    await renderApp("/app/northstar/integrations");
    await user.click(await screen.findByRole("button", { name: "Create integration" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Name"), "SAP production");
    await user.type(within(dialog).getByLabelText("Slug"), "sap-production");
    await user.selectOptions(within(dialog).getByLabelText("Integration type"), "sap_s4hana");
    await user.type(within(dialog).getByLabelText("Endpoint URL"), "https://sap.example.com/odata");
    await user.click(within(dialog).getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(body).toEqual({
        name: "SAP production",
        slug: "sap-production",
        integration_type: "sap_s4hana",
        endpoint_url: "https://sap.example.com/odata",
      }),
    );
    expect(JSON.stringify(body)).not.toContain("secret");
  });
});
