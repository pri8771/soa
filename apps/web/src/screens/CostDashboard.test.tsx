import { HttpResponse, http } from "msw";
import { screen, within } from "@testing-library/react";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/analytics/costs";

describe("Cost dashboard (ANA-006)", () => {
  it("labels estimated vs reconciled (final) and shows billed units as facts", async () => {
    await renderApp(PATH);
    const totals = await screen.findByRole("region", { name: "Totals" });
    expect(within(totals).getByText("Estimated")).toBeInTheDocument();
    expect(within(totals).getByText("$12.00")).toBeInTheDocument();
    expect(within(totals).getByText("Reconciled (final)")).toBeInTheDocument();
    expect(within(totals).getByText("$9.00")).toBeInTheDocument();
    expect(within(totals).getByText("$-3.00")).toBeInTheDocument();

    const table = screen.getByRole("region", { name: "Usage by group" });
    const ocr = within(table).getByText("hosted-ocr").closest("tr") as HTMLElement;
    expect(within(ocr).getByText("120 pages")).toBeInTheDocument();
    const llm = within(table).getByText("local-llm").closest("tr") as HTMLElement;
    expect(within(llm).getByText("512000 tokens")).toBeInTheDocument();
    expect(within(llm).getByText("qwen")).toBeInTheDocument();
    // Providers by catalog name only — no billing identifiers anywhere.
    expect(screen.queryByText(/account/i)).not.toBeInTheDocument();
  });

  it("states the quota-policy absence instead of faking thresholds", async () => {
    await renderApp(PATH);
    expect(await screen.findByText("No budget thresholds yet")).toBeInTheDocument();
    expect(screen.getByText(/no quota policy is configured yet/)).toBeInTheDocument();
  });

  it("an empty window says so", async () => {
    server.use(
      http.get("/api/orgs/:slug/analytics/usage", () =>
        HttpResponse.json({
          window: {
            since: "2026-06-29T00:00:00+00:00",
            until: "2026-07-13T00:00:00+00:00",
            timezone: "UTC",
          },
          groups: [],
          totals: { estimated_cents: 0, adjustment_cents: 0, reconciled_cents: 0 },
          semantics: {},
          quotas: { configured: false, reason: "not configured" },
        }),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText("No usage recorded in this window.")).toBeInTheDocument();
  });
});
