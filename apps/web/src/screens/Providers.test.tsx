import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderApp } from "../test/render";

describe("Providers (AIO-019)", () => {
  it("lists providers with capability, region, health, and EXPLICIT data-policy warnings", async () => {
    await renderApp("/app/northstar/providers");
    expect(await screen.findByText("pdfium-native-text")).toBeInTheDocument();
    // Local providers say plainly that content never leaves.
    expect(screen.getAllByText(/content never leaves \(local-only safe\)/).length).toBeGreaterThan(
      0,
    );
    // Hosted providers carry the explicit warnings, not a euphemism.
    expect(screen.getAllByText(/LEAVES the deployment to a third party/).length).toBeGreaterThan(0);
    expect(screen.getByText(/RETAINS customer content/)).toBeInTheDocument();
    // Health is honestly unknown, with the persisted-attempt note explaining why.
    expect(screen.getAllByText("health: unknown").length).toBeGreaterThan(0);
    expect(screen.getByText(/derived from persisted worker attempts/)).toBeInTheDocument();
  });

  it("shows managed credential state without exposing values or store references", async () => {
    await renderApp("/app/northstar/providers");
    expect((await screen.findAllByText("anthropic-claude")).length).toBeGreaterThan(0);
    expect(screen.getByText(/managed and server-bound/)).toBeInTheDocument();
    expect(screen.getByText(/secret-store references are never displayed/)).toBeInTheDocument();
    expect(screen.getByText("approved")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("secretref://");
  });

  it("previews routing and honours the local-only toggle", async () => {
    const user = userEvent.setup();
    await renderApp("/app/northstar/providers");
    await screen.findByText("pdfium-native-text");

    const preview = screen.getByRole("region", { name: "Routing preview" });
    await user.click(within(preview).getByRole("button", { name: "Preview routing" }));
    expect(await within(preview).findByText("hosted-ocr → pdfium-native-text")).toBeInTheDocument();

    await user.click(within(preview).getByLabelText(/Local-only policy/));
    await user.click(within(preview).getByRole("button", { name: "Preview routing" }));
    expect(
      await within(preview).findByText(/eliminated hosted-ocr: the policy is local-only/),
    ).toBeInTheDocument();
  });

  it("supports write-only rotation and server-side draft validation", async () => {
    const user = userEvent.setup();
    await renderApp("/app/northstar/providers");
    await screen.findByRole("region", { name: "Provider and confidence administration" });

    const secretInput = screen.getByLabelText("Provider API key");
    await user.type(secretInput, "new-provider-secret");
    await user.click(screen.getByRole("button", { name: "Rotate credential" }));
    expect(await screen.findByText("Credential stored")).toBeInTheDocument();
    expect(secretInput).toHaveValue("");
    expect(document.body.textContent).not.toContain("new-provider-secret");

    await user.click(screen.getByRole("button", { name: "Validate" }));
    expect(await screen.findByText("Draft is valid")).toBeInTheDocument();
    expect(
      screen.getByText(/server-side policy, catalog, and credential checks passed/),
    ).toBeInTheDocument();
  });
});
