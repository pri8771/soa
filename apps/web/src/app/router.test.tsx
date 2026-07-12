import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render, screen } from "@testing-library/react";

import { createAppRouter } from "./router";

async function renderAt(path: string) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  await router.load();
  render(<RouterProvider router={router} />);
}

describe("router", () => {
  it("renders the overview at /", async () => {
    await renderAt("/");
    expect(await screen.findByRole("heading", { name: "SOA" })).toBeInTheDocument();
  });

  it("renders a designed not-found state for unknown routes", async () => {
    await renderAt("/does-not-exist");
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to overview" })).toBeInTheDocument();
  });
});
