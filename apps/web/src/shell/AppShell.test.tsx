import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AuthProvider } from "../auth/AuthContext";
import { AppShell, NAV_ITEMS } from "./AppShell";
import { ShellSessionProvider, type ShellSession } from "./ShellContext";

function viewport(width: number) {
  let currentWidth = width;
  const records: Array<{
    query: string;
    matches: boolean;
    listeners: Set<(event: MediaQueryListEvent) => void>;
  }> = [];
  const matches = (query: string) => {
    const maximum = /max-width:\s*(\d+)px/.exec(query)?.[1];
    return maximum === undefined ? false : currentWidth <= Number(maximum);
  };

  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => {
      const record = {
        query,
        matches: matches(query),
        listeners: new Set<(event: MediaQueryListEvent) => void>(),
      };
      records.push(record);
      const add = (listener: (event: MediaQueryListEvent) => void) => {
        record.listeners.add(listener);
      };
      const remove = (listener: (event: MediaQueryListEvent) => void) => {
        record.listeners.delete(listener);
      };
      return {
        get matches() {
          return record.matches;
        },
        media: query,
        onchange: null,
        addListener: add,
        removeListener: remove,
        addEventListener: (_type: "change", listener: (event: MediaQueryListEvent) => void) =>
          add(listener),
        removeEventListener: (_type: "change", listener: (event: MediaQueryListEvent) => void) =>
          remove(listener),
        dispatchEvent: () => true,
      } as unknown as MediaQueryList;
    }),
  );

  return {
    resize(nextWidth: number) {
      currentWidth = nextWidth;
      for (const record of records) {
        const next = matches(record.query);
        if (next === record.matches) continue;
        record.matches = next;
        const event = { matches: next, media: record.query } as MediaQueryListEvent;
        for (const listener of record.listeners) listener(event);
      }
    },
  };
}

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

  afterEach(() => {
    vi.unstubAllGlobals();
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

  it("tracks compact viewport changes until the user chooses a preference", async () => {
    const view = viewport(1440);
    const rendered = await renderShell(["organization.read"]);
    const shell = rendered.container.querySelector(".soa-shell");
    expect(shell).toHaveAttribute("data-nav-collapsed", "false");

    act(() => view.resize(1100));
    await waitFor(() => expect(shell).toHaveAttribute("data-nav-collapsed", "true"));
    expect(screen.getByRole("button", { name: /Expand navigation/ })).toBeInTheDocument();

    act(() => view.resize(1440));
    await waitFor(() => expect(shell).toHaveAttribute("data-nav-collapsed", "false"));
  });

  it("forces a desktop expanded preference closed only on narrow viewports", async () => {
    localStorage.setItem("soa.nav.collapsed", "false");
    const view = viewport(900);
    const rendered = await renderShell(["organization.read"]);
    const shell = rendered.container.querySelector(".soa-shell");
    expect(shell).toHaveAttribute("data-nav-collapsed", "true");
    expect(screen.queryByRole("button", { name: /Expand navigation/ })).not.toBeInTheDocument();

    act(() => view.resize(1100));
    await waitFor(() => expect(shell).toHaveAttribute("data-nav-collapsed", "false"));
    expect(screen.getByRole("button", { name: /Collapse navigation/ })).toBeInTheDocument();
  });

  it("renders breadcrumbs and page title", async () => {
    await renderShell(["organization.read"]);
    expect(await screen.findByRole("navigation", { name: "Breadcrumb" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Overview" })).toBeInTheDocument();
  });
});
