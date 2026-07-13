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
import { ProcessVersions } from "../screens/ProcessVersions";
import { RuleBuilder } from "../screens/RuleBuilder";
import { SchemaBuilder } from "../screens/SchemaBuilder";
import { StreamConfigure } from "../screens/StreamConfigure";
import { StreamDetail } from "../screens/StreamDetail";
import { Streams } from "../screens/Streams";
import { DocumentsQueue } from "../screens/DocumentsQueue";
import { UploadDocuments } from "../screens/UploadDocuments";
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

const schemaBuilderRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "processes/$processSlug/schema",
  component: SchemaBuilder,
});

const ruleBuilderRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "processes/$processSlug/rules",
  component: RuleBuilder,
});

const processVersionsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "processes/$processSlug/versions",
  component: ProcessVersions,
});

const documentsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "documents",
  component: DocumentsQueue,
});

const uploadDocumentsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "documents/upload",
  component: UploadDocuments,
});

const streamsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "streams",
  component: Streams,
});

const streamDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "streams/$streamSlug",
  component: StreamDetail,
});

const streamConfigureRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "streams/$streamSlug/configure",
  component: StreamConfigure,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  workbenchRoute,
  selectOrganizationRoute,
  appRoute.addChildren([
    areaRoute("overview", "Overview", "ANA"),
    documentsRoute,
    uploadDocumentsRoute,
    areaRoute("review", "Review", "REV"),
    processesRoute,
    schemaBuilderRoute,
    ruleBuilderRoute,
    processVersionsRoute,
    streamsRoute,
    streamDetailRoute,
    streamConfigureRoute,
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
