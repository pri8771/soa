import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderApp } from "../test/render";

describe("Support intake (GTM-006)", () => {
  it("shows the support reference and the severity model", async () => {
    await renderApp("/app/northstar/support");

    expect(await screen.findByRole("heading", { name: "Support" })).toBeInTheDocument();
    // A stable, non-secret reference derived from the org slug.
    expect(screen.getByText("SOA-NORTHSTAR")).toBeInTheDocument();
    // The severity model, aligned with the incident-communication severities.
    // (Labels also appear in the severity <Select>, so match by definition.)
    expect(screen.getByText(/Data at risk \(loss or exposure\)/)).toBeInTheDocument();
    expect(screen.getByText(/A core flow — intake, review, or export/)).toBeInTheDocument();
    expect(screen.getByText(/single-user or contained issue/)).toBeInTheDocument();
    // The confidential-data warning is present.
    expect(screen.getByText(/Never email or paste document contents/)).toBeInTheDocument();
  });

  it("composes a copy-ready request from the reference, severity, and summary", async () => {
    const user = userEvent.setup();
    await renderApp("/app/northstar/support");

    const composed = (await screen.findByLabelText(
      "Copy this into your support channel",
    )) as HTMLTextAreaElement;
    expect(composed.value).toContain("Support reference: SOA-NORTHSTAR");
    expect(composed.value).toContain("Severity: SEV3 — Minor");

    await user.type(screen.getByLabelText("Summary"), "Export to NetSuite failing");
    expect(composed.value).toContain("Summary: Export to NetSuite failing");
    // The composer reminds people not to paste confidential data.
    expect(composed.value).toContain("Do NOT paste document contents");
  });
});
