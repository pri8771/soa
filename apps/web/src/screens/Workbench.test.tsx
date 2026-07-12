import { screen } from "@testing-library/react";

import { renderApp } from "../test/render";

const REQUIRED_SECTIONS = [
  "buttons",
  "fields",
  "selection",
  "toggles",
  "overlays",
  "tabs",
  "status",
  "long-text",
];

describe("Workbench", () => {
  it("renders every required component section", async () => {
    const { container } = await renderApp("/workbench");
    await screen.findByRole("heading", { name: "Component workbench" });
    for (const section of REQUIRED_SECTIONS) {
      expect(
        container.querySelector(`[data-workbench-section="${section}"]`),
        `missing workbench section: ${section}`,
      ).not.toBeNull();
    }
  });
});
