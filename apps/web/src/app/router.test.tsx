import { screen, waitFor } from "@testing-library/react";

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

describe("authentication routes", () => {
  it("redirects an anonymous protected route to login and preserves its destination", async () => {
    const { router } = await renderAt("/app/northstar/review?view=mine", {
      authStatus: "anonymous",
    });

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(router.state.location.search).toMatchObject({
      returnTo: "/app/northstar/review?view=mine",
    });
  });

  it("explains session expiry on the login route", async () => {
    await renderAt("/app/northstar/overview", { authStatus: "expired" });
    expect(await screen.findByText("Your session expired")).toBeInTheDocument();
  });
});

describe("app shell routes", () => {
  it("renders the shell with permission-aware navigation at /app/:org/overview", async () => {
    await renderAt("/app/northstar/overview");
    expect(await screen.findByRole("navigation", { name: "Primary" })).toBeInTheDocument();
    expect(screen.getByTestId("current-organization")).toHaveTextContent("Northstar Distribution");
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });

  it("renders organization administration settings", async () => {
    await renderAt("/app/northstar/settings");
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Members and roles" })).toBeInTheDocument();
  });

  it("renders a designed permission-denied state for a restricted deep link", async () => {
    await renderAt("/app/meridian/review");
    expect(await screen.findByRole("heading", { name: "Permission denied" })).toBeInTheDocument();
    expect(screen.getByText("documents.review")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Review queue" })).not.toBeInTheDocument();
  });
});
