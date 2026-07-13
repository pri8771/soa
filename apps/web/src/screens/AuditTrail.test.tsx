import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderApp } from "../test/render";

const PATH = "/app/northstar/audit";

describe("Audit trail (ANA-007)", () => {
  it("lists events and filters by action prefix", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    const table = await screen.findByRole("region", { name: "Audit events" });
    expect(within(table).getByText("document.approved")).toBeInTheDocument();
    expect(within(table).getByText("catalog.match_selected")).toBeInTheDocument();

    await user.type(screen.getByLabelText("Filter by action prefix"), "document.");
    await user.click(screen.getByRole("button", { name: "Apply filter" }));
    await waitFor(() =>
      expect(screen.queryByText("catalog.match_selected")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("document.approved")).toBeInTheDocument();
  });

  it("exports a bundle and shows the integrity manifest hashes", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByRole("region", { name: "Audit events" });
    await user.click(screen.getByRole("button", { name: "Export signed bundle" }));
    const bundle = await screen.findByRole("region", { name: "Export bundle" });
    expect(within(bundle).getByText(/Export e9999999 — 2 event\(s\)/)).toBeInTheDocument();
    expect(within(bundle).getByText("ab".repeat(32))).toBeInTheDocument();
    const file = within(bundle).getByRole("link", { name: "events-0001.ndjson" });
    expect(file).toHaveAttribute("href", expect.stringContaining("sig="));
    expect(within(bundle).getByText("cd".repeat(32))).toBeInTheDocument();
  });
});
