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

describe("app shell routes", () => {
  it("renders the shell with permission-aware navigation at /app/:org/overview", async () => {
    await renderAt("/app/northstar/overview");
    expect(await screen.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByTestId("current-organization")).toHaveTextContent("northstar");
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });

  it("renders placeholder screens for unbuilt areas", async () => {
    await renderAt("/app/northstar/review");
    expect(await screen.findByText("Review is not built yet")).toBeInTheDocument();
  });
});
