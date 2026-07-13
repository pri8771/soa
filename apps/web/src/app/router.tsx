import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  type RouterHistory,
} from "@tanstack/react-router";

import { AppLayout } from "../screens/AppLayout";
import { Home } from "../screens/Home";
import { JobsQueue } from "../screens/JobsQueue";
import { Processes } from "../screens/Processes";
import { NotFound } from "../screens/NotFound";
import { makePlaceholderScreen } from "../screens/Placeholder";
import { SelectOrganization } from "../screens/SelectOrganization";
import { Workbench } from "../screens/Workbench";

const rootRoute = createRootRoute({
  component: () => <Outlet />,
  notFoundComponent: NotFound,
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: Home,
});

const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/workbench",
  component: Workbench,
});

const selectOrganizationRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/select-organization",
  component: SelectOrganization,
});

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/app/$organizationSlug",
  component: AppLayout,
});

// Area screens: placeholders until their owning epics land (see Placeholder).
// The generic keeps each path a literal type so router links stay type-safe.
function areaRoute<const P extends string>(path: P, title: string, epic: string) {
  return createRoute({
    getParentRoute: () => appRoute,
    path,
    component: makePlaceholderScreen(title, epic),
  });
}

const jobsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "jobs",
  component: JobsQueue,
});

const processesRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "processes",
  component: Processes,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  workbenchRoute,
  selectOrganizationRoute,
  appRoute.addChildren([
    areaRoute("overview", "Overview", "ANA"),
    areaRoute("documents", "Documents", "ING"),
    areaRoute("review", "Review", "REV"),
    processesRoute,
    areaRoute("streams", "Streams", "CFG"),
    areaRoute("catalogs", "Catalogs", "CAT"),
    areaRoute("integrations", "Integrations", "EXP"),
    jobsRoute,
    areaRoute("analytics", "Analytics", "ANA"),
    areaRoute("settings", "Settings", "TEN/SEC"),
  ]),
]);

export function createAppRouter(history?: RouterHistory) {
  return createRouter({ routeTree, history });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof createAppRouter>;
  }
}
