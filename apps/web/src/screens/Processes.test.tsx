import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

describe("Processes browser (CFG-009)", () => {
  it("lists processes with status, active version, and counts", async () => {
    await renderApp("/app/northstar/processes");
    expect(await screen.findByText("Purchase orders")).toBeInTheDocument();
    expect(screen.getByText("v3")).toBeInTheDocument();
    expect(screen.getByText("Order confirmations")).toBeInTheDocument();
    expect(screen.getByText("never published")).toBeInTheDocument();
    // Placeholder columns are honest, not fake.
    expect(screen.getAllByText("not tracked yet").length).toBe(2);
  });

  it("keeps the status filter in the URL and filters rows", async () => {
    const user = userEvent.setup();
    const { router } = await renderApp("/app/northstar/processes");
    await screen.findByText("Purchase orders");

    await user.click(screen.getByRole("button", { name: /Status/ }));
    await user.click(await screen.findByRole("option", { name: "Archived" }));

    await waitFor(() =>
      expect((router.state.location.search as { processStatus?: string }).processStatus).toBe(
        "archived",
      ),
    );
    await waitFor(() => expect(screen.queryByText("Purchase orders")).not.toBeInTheDocument());
    expect(screen.getByText("Order confirmations")).toBeInTheDocument();
  });

  it("shows a designed empty state", async () => {
    server.use(http.get("/api/orgs/:slug/processes", () => HttpResponse.json([])));
    await renderApp("/app/northstar/processes");
    expect(await screen.findByText("No processes yet")).toBeInTheDocument();
  });

  it("shows a retryable error state on API failure", async () => {
    server.use(http.get("/api/orgs/:slug/processes", () => HttpResponse.json({}, { status: 500 })));
    await renderApp("/app/northstar/processes");
    expect(await screen.findByText("Couldn’t load processes")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Try again" }).length).toBeGreaterThan(0);
  });
});
