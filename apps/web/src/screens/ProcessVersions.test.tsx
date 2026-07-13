import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PROCESS_VERSION_IDS, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/processes/purchase-orders/versions";

describe("Process version history (CFG-014)", () => {
  it("shows the timeline with states, summaries, and rollback only on superseded", async () => {
    await renderApp(PATH);
    const timeline = await screen.findByRole("region", { name: "Version timeline" });
    expect(await within(timeline).findByText("v3")).toBeInTheDocument();
    expect(within(timeline).getByText("active")).toBeInTheDocument();
    expect(within(timeline).getByText("switch to German")).toBeInTheDocument();
    expect(within(timeline).getByText("raise confidence floor")).toBeInTheDocument();
    // Rollback offered for superseded versions, not the active one.
    expect(within(timeline).getByRole("button", { name: "Roll back to v2" })).toBeInTheDocument();
    expect(
      within(timeline).queryByRole("button", { name: "Roll back to v3" }),
    ).not.toBeInTheDocument();
  });

  it("diffs the active version against its predecessor and redacts secrets", async () => {
    await renderApp(PATH);
    // Default comparison v2 -> v3: language changed, api_token removed.
    const changed = await screen.findByText("changed");
    expect(changed).toBeInTheDocument();
    expect(screen.getByText('"en"')).toBeInTheDocument();
    expect(screen.getByText('"de"')).toBeInTheDocument();
    expect(screen.getByText("removed")).toBeInTheDocument();
    // The secret value NEVER appears — only the redaction marker.
    expect(screen.queryByText(/sk-live-verysecret/)).not.toBeInTheDocument();
    expect(screen.getByText("•••••• (redacted)")).toBeInTheDocument();
  });

  it("comparing other versions shows added settings", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByRole("region", { name: "Compare versions" });
    await user.click(screen.getByRole("button", { name: /From version/ }));
    await user.click(await screen.findByRole("option", { name: "v1" }));
    expect(await screen.findByText("added")).toBeInTheDocument();
    expect(screen.getByText("confidence_floor")).toBeInTheDocument();
  });

  it("rollback demands a reason and posts it to the audited endpoint", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/processes/purchase-orders/rollback", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({
          id: "31111111-1111-4111-8111-111111111111",
          name: "Purchase orders",
          slug: "purchase-orders",
          status: "active",
          active_version_id: PROCESS_VERSION_IDS.v2,
          version: 5,
        });
      }),
    );
    await renderApp(PATH);
    await user.click(await screen.findByRole("button", { name: "Roll back to v2" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Roll back" });
    expect(confirm).toBeDisabled();
    await user.type(
      within(dialog).getByLabelText(/Reason for rolling back/),
      "v3 broke German exports",
    );
    await user.click(confirm);
    await waitFor(() =>
      expect(posted).toEqual({
        target_version_id: PROCESS_VERSION_IDS.v2,
        reason: "v3 broke German exports",
      }),
    );
  });

  it("a refused rollback surfaces the server's reason", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/orgs/northstar/processes/purchase-orders/rollback", () =>
        HttpResponse.json(
          { error: { message: "only published or superseded versions can become active" } },
          { status: 409 },
        ),
      ),
    );
    await renderApp(PATH);
    await user.click(await screen.findByRole("button", { name: "Roll back to v2" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.type(within(dialog).getByLabelText(/Reason for rolling back/), "testing refusal");
    await user.click(within(dialog).getByRole("button", { name: "Roll back" }));
    expect(await screen.findByText("Rollback refused")).toBeInTheDocument();
    expect(screen.getByText(/can become active/)).toBeInTheDocument();
  });
});
