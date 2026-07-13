import { HttpResponse, http } from "msw";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

/**
 * A comparison whose AGGREGATE improves while one critical field and the
 * false-auto-approval rate regress — the case averages must never hide.
 */
const COMPARISON = {
  available: true,
  comparison: {
    current_label: "v3 (live)",
    candidate_label: "v4 (candidate)",
    field_exact_rate: { current: 0.82, candidate: 0.9 },
    field_normalized_rate: { current: 0.88, candidate: 0.94 },
    false_auto_approval_rate: { current: 0.0, candidate: 0.04 },
    review_rate: { current: 0.4, candidate: 0.3 },
    total_cost_cents: { current: 220, candidate: 180 },
    findings: [
      {
        kind: "critical_field_regression",
        detail:
          "critical field 'po_number' regressed from 99.00% to 95.00% — prohibited, no waiver applies",
        waivable: false,
      },
      {
        kind: "false_auto_approval",
        detail:
          "the false-auto-approval rate rose from 0.00% to 4.00% — the candidate would auto-approve more documents containing wrong values; prohibited",
        waivable: false,
      },
      {
        kind: "field_regression",
        detail: "field 'notes' regressed from 70.00% to 60.00% (tolerance 2.00%)",
        waivable: true,
      },
    ],
    field_diffs: [
      {
        field: "po_number",
        current_exact_rate: 0.99,
        candidate_exact_rate: 0.95,
        delta: -0.04,
      },
      { field: "total", current_exact_rate: 0.8, candidate_exact_rate: 0.92, delta: 0.12 },
    ],
    cohort_diffs: [
      { cohort: "test", current_exact_rate: 0.85, candidate_exact_rate: 0.91, delta: 0.06 },
      {
        cohort: "validation",
        current_exact_rate: 0.8,
        candidate_exact_rate: 0.89,
        delta: 0.09,
      },
    ],
    documents: [
      {
        document_sha256: "a".repeat(64),
        split: "test",
        wrong_fields: ["po_number", "total"],
        auto_approved: true,
      },
      {
        document_sha256: "b".repeat(64),
        split: "validation",
        wrong_fields: [],
        auto_approved: false,
      },
    ],
  },
};

function serveComparison() {
  server.use(
    http.get("/api/orgs/northstar/streams/:streamSlug/simulation", () =>
      HttpResponse.json(COMPARISON),
    ),
  );
}

describe("Simulation (AIO-018)", () => {
  it("is honest while no evaluation runs exist", async () => {
    await renderApp("/app/northstar/streams/email-eu/simulation");
    expect(await screen.findByText("No evaluation runs yet")).toBeInTheDocument();
    expect(screen.getByText(/results appear here once run storage lands/)).toBeInTheDocument();
  });

  it("shows critical regressions FIRST even when the aggregate improved", async () => {
    serveComparison();
    await renderApp("/app/northstar/streams/email-eu/simulation");
    const banner = await screen.findByText("Critical regression — publication blocked");
    expect(banner).toBeInTheDocument();
    expect(screen.getByText(/critical field 'po_number' regressed/)).toBeInTheDocument();
    expect(screen.getByText(/false-auto-approval rate rose/)).toBeInTheDocument();
    // The aggregate DID improve — and the banner is still there.
    const aggregate = screen.getByRole("group", { name: "Field accuracy (exact)" });
    expect(within(aggregate).getByText("82.0% → 90.0%")).toBeInTheDocument();
    expect(within(aggregate).getByText("+8.0 pp")).toBeInTheDocument();
  });

  it("renders accessible per-field and per-cohort tables carrying the chart numbers", async () => {
    serveComparison();
    await renderApp("/app/northstar/streams/email-eu/simulation");
    await screen.findByText("Critical regression — publication blocked");

    const fieldTable = screen.getByRole("table", { name: /Per-field exact accuracy/ });
    const poRow = within(fieldTable).getByRole("rowheader", { name: "po_number" }).closest("tr");
    expect(poRow).not.toBeNull();
    expect(within(poRow as HTMLElement).getByText("99.0%")).toBeInTheDocument();
    expect(within(poRow as HTMLElement).getByText("95.0%")).toBeInTheDocument();
    expect(within(poRow as HTMLElement).getByText("-4.0 pp")).toBeInTheDocument();

    const cohortTable = screen.getByRole("table", { name: /Per-cohort exact accuracy/ });
    expect(within(cohortTable).getByRole("rowheader", { name: "validation" })).toBeInTheDocument();

    // Aggregate cards include cost and review-rate changes.
    expect(screen.getByRole("group", { name: "Cost per run" })).toHaveTextContent(
      "2.20 € → 1.80 €",
    );
    expect(screen.getByRole("group", { name: "Review rate" })).toHaveTextContent("40.0% → 30.0%");
  });

  it("drills down to the documents the candidate got wrong", async () => {
    const user = userEvent.setup();
    serveComparison();
    await renderApp("/app/northstar/streams/email-eu/simulation");
    await screen.findByText("Critical regression — publication blocked");

    const drilldown = screen.getByRole("region", { name: "Document drill-down" });
    expect(within(drilldown).getByText("would auto-approve despite errors")).toBeInTheDocument();
    await user.click(within(drilldown).getByText(/aaaaaaaaaaaa/));
    expect(within(drilldown).getByText(/Wrong fields: po_number, total/)).toBeInTheDocument();
    // The clean document does not appear in the wrong list.
    expect(within(drilldown).queryByText(/bbbbbbbbbbbb/)).not.toBeInTheDocument();
  });
});
