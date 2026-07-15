import {
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  type RouterHistory,
} from "@tanstack/react-router";
import { lazy, Suspense, type ComponentType } from "react";

import { AppLayout } from "../screens/AppLayout";
import { NotFound } from "../screens/NotFound";
import { makePlaceholderScreen } from "../screens/Placeholder";

function lazyScreen(loader: () => Promise<{ default: ComponentType }>) {
  const Screen = lazy(loader);
  return function LazyScreen() {
    return (
      <Suspense fallback={<div role="status">Loading page…</div>}>
        <Screen />
      </Suspense>
    );
  };
}

const Home = lazyScreen(() => import("../screens/Home").then(({ Home }) => ({ default: Home })));
const Workbench = lazyScreen(() =>
  import("../screens/Workbench").then(({ Workbench }) => ({ default: Workbench })),
);
const SelectOrganization = lazyScreen(() =>
  import("../screens/SelectOrganization").then(({ SelectOrganization }) => ({
    default: SelectOrganization,
  })),
);
const JobsQueue = lazyScreen(() =>
  import("../screens/JobsQueue").then(({ JobsQueue }) => ({ default: JobsQueue })),
);
const Processes = lazyScreen(() =>
  import("../screens/Processes").then(({ Processes }) => ({ default: Processes })),
);
const ProcessVersions = lazyScreen(() =>
  import("../screens/ProcessVersions").then(({ ProcessVersions }) => ({
    default: ProcessVersions,
  })),
);
const RuleBuilder = lazyScreen(() =>
  import("../screens/RuleBuilder").then(({ RuleBuilder }) => ({ default: RuleBuilder })),
);
const SchemaBuilder = lazyScreen(() =>
  import("../screens/SchemaBuilder").then(({ SchemaBuilder }) => ({ default: SchemaBuilder })),
);
const StreamConfigure = lazyScreen(() =>
  import("../screens/StreamConfigure").then(({ StreamConfigure }) => ({
    default: StreamConfigure,
  })),
);
const StreamDetail = lazyScreen(() =>
  import("../screens/StreamDetail").then(({ StreamDetail }) => ({ default: StreamDetail })),
);
const Streams = lazyScreen(() =>
  import("../screens/Streams").then(({ Streams }) => ({ default: Streams })),
);
const DocumentDetail = lazyScreen(() =>
  import("../screens/DocumentDetail").then(({ DocumentDetail }) => ({ default: DocumentDetail })),
);
const DocumentsQueue = lazyScreen(() =>
  import("../screens/DocumentsQueue").then(({ DocumentsQueue }) => ({
    default: DocumentsQueue,
  })),
);
const GettingStarted = lazyScreen(() =>
  import("../screens/GettingStarted").then(({ GettingStarted }) => ({
    default: GettingStarted,
  })),
);
const Support = lazyScreen(() =>
  import("../screens/Support").then(({ Support }) => ({ default: Support })),
);
const Integrations = lazyScreen(() =>
  import("../screens/Integrations").then(({ Integrations }) => ({ default: Integrations })),
);
const MappingStudio = lazyScreen(() =>
  import("../screens/MappingStudio").then(({ MappingStudio }) => ({ default: MappingStudio })),
);
const ReviewQueue = lazyScreen(() =>
  import("../screens/ReviewQueue").then(({ ReviewQueue }) => ({ default: ReviewQueue })),
);
const ReviewStudio = lazyScreen(() =>
  import("../screens/ReviewStudio").then(({ ReviewStudio }) => ({ default: ReviewStudio })),
);
const UploadDocuments = lazyScreen(() =>
  import("../screens/UploadDocuments").then(({ UploadDocuments }) => ({
    default: UploadDocuments,
  })),
);
const CatalogManager = lazyScreen(() =>
  import("../screens/CatalogManager").then(({ CatalogManager }) => ({ default: CatalogManager })),
);
const Catalogs = lazyScreen(() =>
  import("../screens/Catalogs").then(({ Catalogs }) => ({ default: Catalogs })),
);
const Providers = lazyScreen(() =>
  import("../screens/Providers").then(({ Providers }) => ({ default: Providers })),
);
const AuditTrail = lazyScreen(() =>
  import("../screens/AuditTrail").then(({ AuditTrail }) => ({ default: AuditTrail })),
);
const CostDashboard = lazyScreen(() =>
  import("../screens/CostDashboard").then(({ CostDashboard }) => ({ default: CostDashboard })),
);
const Operations = lazyScreen(() =>
  import("../screens/Operations").then(({ Operations }) => ({ default: Operations })),
);
const QualityDashboard = lazyScreen(() =>
  import("../screens/QualityDashboard").then(({ QualityDashboard }) => ({
    default: QualityDashboard,
  })),
);
const Simulation = lazyScreen(() =>
  import("../screens/Simulation").then(({ Simulation }) => ({ default: Simulation })),
);

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

const documentDetailRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "documents/$documentId",
  component: DocumentDetail,
});

const uploadDocumentsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "documents/upload",
  component: UploadDocuments,
});

const reviewRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "review",
  component: ReviewQueue,
});

const reviewStudioRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "review/$taskId",
  component: ReviewStudio,
});

const integrationsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "integrations",
  component: Integrations,
});

const mappingStudioRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "integrations/$integrationSlug",
  component: MappingStudio,
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

const catalogsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "catalogs",
  component: Catalogs,
});

const catalogManagerRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "catalogs/$catalogSlug",
  component: CatalogManager,
});

const providersRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "providers",
  component: Providers,
});

const simulationRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "streams/$streamSlug/simulation",
  component: Simulation,
});

const operationsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "overview",
  component: Operations,
});

const gettingStartedRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "getting-started",
  component: GettingStarted,
});

const supportRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "support",
  component: Support,
});

const qualityRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "analytics",
  component: QualityDashboard,
});

const costsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "analytics/costs",
  component: CostDashboard,
});

const auditRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "audit",
  component: AuditTrail,
});

const routeTree = rootRoute.addChildren([
  indexRoute,
  workbenchRoute,
  selectOrganizationRoute,
  appRoute.addChildren([
    operationsRoute,
    gettingStartedRoute,
    supportRoute,
    documentsRoute,
    uploadDocumentsRoute,
    documentDetailRoute,
    reviewRoute,
    reviewStudioRoute,
    processesRoute,
    schemaBuilderRoute,
    ruleBuilderRoute,
    processVersionsRoute,
    streamsRoute,
    streamDetailRoute,
    streamConfigureRoute,
    simulationRoute,
    providersRoute,
    catalogsRoute,
    catalogManagerRoute,
    integrationsRoute,
    mappingStudioRoute,
    jobsRoute,
    qualityRoute,
    costsRoute,
    auditRoute,
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
