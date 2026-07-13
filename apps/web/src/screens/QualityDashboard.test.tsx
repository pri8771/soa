import { HttpResponse, http } from "msw";
import { screen, within } from "@testing-library/react";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/analytics";

describe("Quality dashboard (ANA-005)", () => {
  it("shows rates WITH their sample sizes and the proxy banner", async () => {
    await renderApp(PATH);
    // The proxy caveat is a banner, not a footnote.
    expect(
      await screen.findByText("These are correction-based proxies, not accuracy"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Gold documents available: 0/)).toBeInTheDocument();
    // The window and the sample size are stated up front.
    expect(screen.getByText(/Sample: 12 review task\(s\) completed/)).toBeInTheDocument();

    const fields = screen.getByRole("region", { name: "Field corrections" });
    const po = within(fields).getByText("po_number").closest("tr") as HTMLElement;
    expect(within(po).getByText("3/12")).toBeInTheDocument();
    expect(within(po).getByText("25.0%")).toBeInTheDocument();
    expect(within(fields).getByText(/Line cells: 6\/48 corrected \(12.5%\)/)).toBeInTheDocument();

    const stp = screen.getByRole("region", { name: "Straight-through processing" });
    expect(within(stp).getByText(/8\/20 settled without a human \(40.0%\)/)).toBeInTheDocument();
  });

  it("declares false auto-approval unmeasurable and marks unmeasurable cohorts", async () => {
    await renderApp(PATH);
    const section = await screen.findByRole("region", { name: "False auto-approval" });
    expect(within(section).getByText("Not measurable from production data")).toBeInTheDocument();
    expect(within(section).getByText(/gold dataset/)).toBeInTheDocument();

    const calibration = screen.getByRole("region", { name: "Calibration" });
    const emptyCohort = within(calibration).getByText("[0.0, 0.5)").closest("tr") as HTMLElement;
    // An unmeasured cohort says so — it never renders as a flattering 0%.
    expect(within(emptyCohort).getByText("not measurable")).toBeInTheDocument();
    // Small samples carry a visible caveat.
    const smallCohort = within(calibration).getByText("[0.5, 0.8)").closest("tr") as HTMLElement;
    expect(within(smallCohort).getByText("small sample")).toBeInTheDocument();
    const bigCohort = within(calibration).getByText("[0.95, 1.0]").closest("tr") as HTMLElement;
    expect(within(bigCohort).queryByText("small sample")).not.toBeInTheDocument();
  });

  it("an unavailable analytics service fails loudly with a retry", async () => {
    server.use(
      http.get("/api/orgs/:slug/analytics/quality", () =>
        HttpResponse.json({ error: { message: "boom" } }, { status: 500 }),
      ),
    );
    await renderApp(PATH);
    expect(await screen.findByText("Couldn’t load the quality snapshot")).toBeInTheDocument();
  });
});
