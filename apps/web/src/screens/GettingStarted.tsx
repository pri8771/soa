/**
 * Guided onboarding checklist (GTM-003).
 *
 * The organization → process/schema → stream → catalog → sample →
 * integration → go-live path, as a RESUMABLE checklist: each step's
 * status is derived from live data (not a stored flag), so it reflects
 * exactly where the tenant actually is, and every item links to the
 * precise screen that completes it. Nothing here mutates state — it
 * reads the same endpoints the feature screens do and points the way.
 */

import { Badge, Banner, ProgressBar } from "@soa/design-system";
import { useQueries } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { CSSProperties, ReactNode } from "react";

import {
  fetchCatalogs,
  fetchDocuments,
  fetchIntegrations,
  fetchProcesses,
  fetchStreams,
} from "../api/client";
import { DASHBOARD_POLL_MS, liveQueryOptions } from "../app/liveQuery";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

type StepStatus = "done" | "todo" | "pending";

interface ChecklistStep {
  key: string;
  title: string;
  description: string;
  status: StepStatus;
  /** The screen that completes this step. */
  link: ReactNode;
}

const card: CSSProperties = {
  border: "1px solid var(--soa-border)",
  borderRadius: "var(--soa-radius-panel)",
  padding: "var(--soa-space-4)",
  display: "flex",
  gap: "var(--soa-space-3)",
  alignItems: "flex-start",
  flexWrap: "wrap",
};

function statusBadge(status: StepStatus): ReactNode {
  if (status === "pending") return <Badge tone="neutral">checking…</Badge>;
  if (status === "done") return <Badge tone="success">done</Badge>;
  return <Badge tone="warning">to do</Badge>;
}

export function GettingStarted() {
  const session = useShellSession();
  const slug = session.organization.slug;

  const live = liveQueryOptions(DASHBOARD_POLL_MS);
  const [processes, streams, catalogs, integrations, documents] = useQueries({
    queries: [
      { queryKey: ["processes", slug], queryFn: () => fetchProcesses(slug), ...live },
      { queryKey: ["streams", slug], queryFn: () => fetchStreams(slug), ...live },
      { queryKey: ["catalogs", slug], queryFn: () => fetchCatalogs(slug), ...live },
      { queryKey: ["integrations", slug], queryFn: () => fetchIntegrations(slug), ...live },
      { queryKey: ["documents", slug, "onboarding"], queryFn: () => fetchDocuments(slug), ...live },
    ],
  });

  // A step is `pending` while its query loads, `done` when the live data
  // satisfies it, else `todo`. Any query error is treated as not-yet-done
  // (the linked screen surfaces the real error).
  const derive = (loading: boolean, done: boolean): StepStatus =>
    loading ? "pending" : done ? "done" : "todo";

  const linkStyle: CSSProperties = {
    border: "1px solid var(--soa-border)",
    borderRadius: "var(--soa-radius-control)",
    padding: "0.375rem 0.75rem",
    fontSize: "0.85rem",
    textDecoration: "none",
    color: "inherit",
    whiteSpace: "nowrap",
  };

  const link = (to: string, params: Record<string, string>, label: string): ReactNode => (
    <Link to={to} params={params} style={linkStyle}>
      {label}
    </Link>
  );

  const orgParams = { organizationSlug: slug };
  const hasActiveProcess = (processes.data ?? []).some((p) => p.active_version_id);
  const hasStream = (streams.data ?? []).length > 0;
  const hasActiveCatalog = (catalogs.data?.items ?? []).some((c) => c.active_version_id);
  const hasIntegration = (integrations.data?.items ?? []).length > 0;
  const hasDocument = (documents.data?.items ?? []).length > 0;

  const steps: ChecklistStep[] = [
    {
      key: "organization",
      title: "Create your organization",
      description: "You're in — your organization is the tenant boundary for everything below.",
      status: "done",
      link: link("/app/$organizationSlug/settings", orgParams, "Organization settings"),
    },
    {
      key: "process",
      title: "Publish a process and schema",
      description:
        "Define what to extract: header fields and line items, with a published schema version.",
      status: derive(processes.isPending, hasActiveProcess),
      link: link("/app/$organizationSlug/processes", orgParams, "Open Processes"),
    },
    {
      key: "stream",
      title: "Set up a stream",
      description: "A stream is the operational bucket documents flow through, with its overrides.",
      status: derive(streams.isPending, hasStream),
      link: link("/app/$organizationSlug/streams", orgParams, "Open Streams"),
    },
    {
      key: "catalog",
      title: "Import a catalog",
      description:
        "Load your customers, ship-tos, and materials so matching can validate against real data.",
      status: derive(catalogs.isPending, hasActiveCatalog),
      link: link("/app/$organizationSlug/catalogs", orgParams, "Open Catalogs"),
    },
    {
      key: "sample",
      title: "Process a sample document",
      description: "Upload a real purchase order end-to-end to see extraction, review, and export.",
      status: derive(documents.isPending, hasDocument),
      link: link("/app/$organizationSlug/documents/upload", orgParams, "Upload a document"),
    },
    {
      key: "integration",
      title: "Connect an integration",
      description:
        "Point exports at your ERP or a signed webhook so approved orders land where they belong.",
      status: derive(integrations.isPending, hasIntegration),
      link: link("/app/$organizationSlug/integrations", orgParams, "Open Integrations"),
    },
  ];

  const goLiveReady = steps.every((s) => s.status === "done");
  const doneCount = steps.filter((s) => s.status === "done").length;

  return (
    <AppShell
      title="Getting started"
      breadcrumbs={[{ label: session.organization.name }, { label: "Getting started" }]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-4)", maxWidth: "56rem" }}>
        <div style={{ display: "grid", gap: "0.5rem" }}>
          <ProgressBar
            value={doneCount}
            maxValue={steps.length}
            label={`Setup progress: ${doneCount} of ${steps.length} steps`}
          />
          <span style={{ color: "var(--soa-text-muted)", fontSize: "0.85rem" }}>
            This checklist reflects your live configuration — finish a step anywhere in the app and
            it updates here. Nothing is stored separately.
          </span>
        </div>

        {goLiveReady ? (
          <Banner tone="success" title="You're ready to go live">
            Every step is complete. Start sending real documents through your stream — the Overview
            dashboard tracks throughput, quality, and exceptions from here.
          </Banner>
        ) : null}

        <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.75rem" }}>
          {steps.map((step, index) => (
            <li key={step.key} style={card}>
              <span
                aria-hidden="true"
                style={{
                  fontWeight: 600,
                  color: "var(--soa-text-muted)",
                  minWidth: "1.5rem",
                }}
              >
                {index + 1}
              </span>
              <div style={{ flex: 1, minWidth: "16rem", display: "grid", gap: "0.25rem" }}>
                <strong>{step.title}</strong>
                <span style={{ color: "var(--soa-text-muted)", fontSize: "0.9rem" }}>
                  {step.description}
                </span>
              </div>
              {statusBadge(step.status)}
              {step.status === "done" ? null : step.link}
            </li>
          ))}
          <li key="go-live" style={card}>
            <span
              aria-hidden="true"
              style={{ fontWeight: 600, color: "var(--soa-text-muted)", minWidth: "1.5rem" }}
            >
              {steps.length + 1}
            </span>
            <div style={{ flex: 1, minWidth: "16rem", display: "grid", gap: "0.25rem" }}>
              <strong>Go live</strong>
              <span style={{ color: "var(--soa-text-muted)", fontSize: "0.9rem" }}>
                Complete the steps above, then start processing production documents.
              </span>
            </div>
            {statusBadge(goLiveReady ? "done" : "todo")}
          </li>
        </ol>
      </div>
    </AppShell>
  );
}
