import { screen } from "@testing-library/react";

import { renderApp as renderAt } from "../test/render";

describe("router", () => {
  it("renders the overview at /", async () => {
    await renderAt("/");
    expect(await screen.findByRole("heading", { name: "SOA" })).toBeInTheDocument();
  });

  it("renders a designed not-found state for unknown routes", async () => {
    await renderAt("/does-not-exist");
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to home" })).toBeInTheDocument();
  });
});

describe("app shell routes", () => {
  it("renders the shell with permission-aware navigation at /app/:org/overview", async () => {
    await renderAt("/app/northstar/overview");
    expect(await screen.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByTestId("current-organization")).toHaveTextContent("Northstar Distribution");
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });

  it("renders placeholder screens for unbuilt areas", async () => {
    await renderAt("/app/northstar/catalogs");
    expect(await screen.findByText("Catalogs is not built yet")).toBeInTheDocument();
  });
});
