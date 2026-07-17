import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

describe("organization entry flows", () => {
  it("creates an organization and derives an editable slug", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    server.use(
      http.post("/api/organizations", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(
          { id: "org-3", name: "Pilot West", slug: "pilot-west", status: "active", version: 1 },
          { status: 201 },
        );
      }),
    );
    const { router } = await renderApp("/create-organization");
    await user.type(await screen.findByLabelText("Organization name"), "Pilot West");
    expect(screen.getByLabelText("Organization slug")).toHaveValue("pilot-west");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    await waitFor(() => expect(body).toEqual({ name: "Pilot West", slug: "pilot-west" }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/app/pilot-west/getting-started"),
    );
  });

  it("accepts an invitation for the signed-in email", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    server.use(
      http.post("/api/invitations/accept", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({
          membership_id: "m-accepted",
          user_id: "u-1",
          invited_email: "reviewer@northstar.example",
          status: "active",
          version: 2,
        });
      }),
    );
    const { router } = await renderApp("/accept-invitation?organization=pilot-east");
    await user.click(await screen.findByRole("button", { name: "Accept invitation" }));

    await waitFor(() => expect(body).toEqual({ organization_slug: "pilot-east" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/app/pilot-east/skills"));
  });
});
