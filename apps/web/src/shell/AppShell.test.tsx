import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AuthProvider } from "../auth/AuthContext";
import { AppShell, NAV_ITEMS } from "./AppShell";
import { ShellSessionProvider, type ShellSession } from "./ShellContext";

function makeSession(permissions: string[]): ShellSession {
  return {
    organization: { slug: "northstar", name: "Northstar Distribution" },
    permissions: new Set(permissions),
    devSession: true,
    userLabel: "reviewer@northstar.example",
    userId: "u-1",
  };
}

async function renderShell(permissions: string[]) {
  const rootRoute = createRootRoute({
    component: () => (
      <ShellSessionProvider session={makeSession(permissions)}>
        <AppShell title="Overview" breadcrumbs={[{ label: "Northstar" }, { label: "Overview" }]}>
          <p>content</p>
        </AppShell>
      </ShellSessionProvider>
    ),
  });
  const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/" });
  const router = createRouter({
    routeTree: rootRoute.addChildren([indexRoute]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  await router.load();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider testStatus="test">
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("shows only navigation items the principal is permitted to use", async () => {
    await renderShell(["organization.read", "documents.read", "documents.review"]);
    await screen.findByRole("navigation", { name: "Primary" });
    expect(screen.getByRole("link", { name: /Overview/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Documents/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Review/ })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Settings/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Integrations/ })).not.toBeInTheDocument();
  });

  it("renders every item for a full-permission principal", async () => {
    await renderShell(NAV_ITEMS.map((item) => item.permission));
    await screen.findByRole("navigation", { name: "Primary" });
    for (const item of NAV_ITEMS) {
      expect(screen.getByRole("link", { name: new RegExp(item.label) })).toBeInTheDocument();
    }
  });

  it("always displays the current organization and dev-session badge", async () => {
    await renderShell(["organization.read"]);
    expect(await screen.findByTestId("current-organization")).toHaveTextContent(
      "Northstar Distribution",
    );
    expect(screen.getByText("Development identity")).toBeInTheDocument();
  });

  it("collapse toggle persists across renders", async () => {
    const user = userEvent.setup();
    await renderShell(["organization.read"]);
    const toggle = await screen.findByRole("button", { name: /Collapse navigation/ });
    await user.click(toggle);
    await waitFor(() => expect(localStorage.getItem("soa.nav.collapsed")).toBe("true"));
    expect(screen.getByRole("button", { name: /Expand navigation/ })).toBeInTheDocument();
  });

  it("renders breadcrumbs and page title", async () => {
    await renderShell(["organization.read"]);
    expect(await screen.findByRole("navigation", { name: "Breadcrumb" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });
});
