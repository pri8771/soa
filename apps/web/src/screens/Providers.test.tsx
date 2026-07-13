import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderApp } from "../test/render";

describe("Providers (AIO-019)", () => {
  it("lists providers with capability, region, health, and EXPLICIT data-policy warnings", async () => {
    await renderApp("/app/northstar/providers");
    expect(await screen.findByText("pdfium-native-text")).toBeInTheDocument();
    // Local providers say plainly that content never leaves.
    expect(screen.getByText(/content never leaves \(local-only safe\)/)).toBeInTheDocument();
    // Hosted providers carry the explicit warnings, not a euphemism.
    expect(screen.getByText(/LEAVES the deployment to a third party/)).toBeInTheDocument();
    expect(screen.getByText(/RETAINS customer content/)).toBeInTheDocument();
    // Health is honestly unknown, with the note explaining why.
    expect(screen.getAllByText("health: unknown").length).toBeGreaterThan(0);
    expect(screen.getByText(/reported by the worker at runtime/)).toBeInTheDocument();
  });

  it("shows the approved provider's credential REFERENCE and states secrets are never displayed", async () => {
    await renderApp("/app/northstar/providers");
    expect(await screen.findByText("hosted-ocr")).toBeInTheDocument();
    expect(screen.getByText("credential:hosted-ocr-main")).toBeInTheDocument();
    expect(screen.getByText(/secret\s+itself is never displayed/)).toBeInTheDocument();
    expect(screen.getByText("approved")).toBeInTheDocument();
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
});
