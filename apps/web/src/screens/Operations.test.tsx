import { HttpResponse, http } from "msw";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/overview";

describe("Operations dashboard (ANA-004)", () => {
  it("every needs-attention tile shows its count and links to the filtered view", async () => {
    await renderApp(PATH);
    const attention = await screen.findByRole("region", { name: "Needs attention" });

    const overdue = within(attention).getByRole("link", {
      name: /Review tasks past their SLA: 1/,
    });
    expect(overdue).toHaveAttribute("href", expect.stringContaining("/app/northstar/review"));
    expect(overdue).toHaveAttribute("href", expect.stringContaining("view=overdue"));

    const quarantined = within(attention).getByRole("link", {
      name: /Quarantined documents: 1/,
    });
    expect(quarantined).toHaveAttribute("href", expect.stringContaining("docState=quarantined"));

    const exports = within(attention).getByRole("link", {
      name: /Export jobs failing in the window: 1/,
    });
    expect(exports).toHaveAttribute("href", expect.stringContaining("/app/northstar/integrations"));

    // Queue health links to the jobs queue — a destination, not a number.
    expect(within(attention).getByRole("link", { name: /Job queue health/ })).toHaveAttribute(
      "href",
      expect.stringContaining("/app/northstar/jobs"),
    );
  });

  it("shows windowed metrics with their sample sizes and honest empties", async () => {
    await renderApp(PATH);
    const processing = await screen.findByRole("region", { name: "Processing" });
    // Volume total = sum of per-day counts, linked to the documents queue.
    expect(
      within(processing).getByRole("link", { name: /received in the window: 6/i }),
    ).toBeInTheDocument();
    // Latency names its sample size.
    expect(within(processing).getByText(/6 runs measured/)).toBeInTheDocument();
    expect(within(processing).getByText(/p50 390 ms · p95 940 ms/)).toBeInTheDocument();
    // SLA shows numerator/denominator, not a bare percentage.
    expect(within(processing).getByText(/1\/3 active overdue/)).toBeInTheDocument();

    const exceptions = screen.getByRole("region", { name: "Exceptions" });
    expect(within(exceptions).getByText("quarantined")).toBeInTheDocument();
  });

  it("definitions are one disclosure away", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByRole("region", { name: "Needs attention" });
    await user.click(screen.getByText("What these numbers mean"));
    expect(screen.getByText("sla.overdue_now")).toBeInTheDocument();
    expect(screen.getByText(/open\/in_progress tasks with sla_due_at < now/)).toBeInTheDocument();
  });

  it("an unavailable analytics service fails loudly with a retry", async () => {
    server.use(
      http.get("/api/orgs/:slug/analytics/operations", () =>
        HttpResponse.json({ error: { message: "boom" } }, { status: 500 }),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText("Couldn’t load the operations snapshot")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
