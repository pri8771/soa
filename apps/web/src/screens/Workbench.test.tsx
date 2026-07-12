import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";

import { createAppRouter } from "../app/router";

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
    const router = createAppRouter(createMemoryHistory({ initialEntries: ["/workbench"] }));
    await router.load();
    const { container } = render(<RouterProvider router={router} />);
    await screen.findByRole("heading", { name: "Component workbench" });
    for (const section of REQUIRED_SECTIONS) {
      expect(
        container.querySelector(`[data-workbench-section="${section}"]`),
        `missing workbench section: ${section}`,
      ).not.toBeNull();
    }
  });
});
