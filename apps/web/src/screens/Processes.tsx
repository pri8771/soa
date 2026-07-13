/**
 * Process browser (CFG-009, UI_UX_BLUEPRINT §6.2).
 *
 * Lists the organization's processes with status, active version, stream
 * and draft counts. Owner and health are placeholder columns until their
 * owning epics land (health arrives with ANA). The status filter lives in
 * the URL so filtered views are shareable and survive refresh.
 */

import { Badge, Banner, Button, Select } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";

import { fetchProcesses, type ProcessSummary } from "../api/client";
import { DataTable } from "../components/table/DataTable";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const STATUS_OPTIONS = [
  { id: "all", label: "All statuses" },
  { id: "active", label: "Active" },
  { id: "archived", label: "Archived" },
];

const COLUMNS: ColumnDef<ProcessSummary>[] = [
  { header: "Process", accessorKey: "name" },
  {
    header: "Status",
    accessorKey: "status",
    cell: ({ row }) => (
      <Badge tone={row.original.status === "active" ? "success" : "neutral"}>
        {row.original.status}
      </Badge>
    ),
  },
  {
    header: "Active version",
    cell: ({ row }) =>
      row.original.active_version_number !== null ? (
        `v${row.original.active_version_number}`
      ) : (
        <Badge tone="warning">never published</Badge>
      ),
  },
  { header: "Streams", accessorKey: "streams_count" },
  {
    header: "Drafts",
    cell: ({ row }) => (row.original.draft_count > 0 ? row.original.draft_count : "—"),
  },
  // Placeholder columns: populated by later epics (owner: TEN follow-up,
  // health: ANA). Rendered honestly as unavailable, never as fake data.
  { header: "Owner", cell: () => "—" },
  {
    id: "links",
    header: "Configure",
    cell: ({ row }) => <ConfigureLinks processSlug={row.original.slug} />,
  },
  { header: "Health", cell: () => <Badge tone="neutral">not tracked yet</Badge> },
];

function ConfigureLinks({ processSlug }: { processSlug: string }) {
  const session = useShellSession();
  const params = { organizationSlug: session.organization.slug, processSlug };
  return (
    <span style={{ display: "inline-flex", gap: "var(--soa-space-3)" }}>
      <Link to="/app/$organizationSlug/processes/$processSlug/schema" params={params}>
        Schema
      </Link>
      <Link to="/app/$organizationSlug/processes/$processSlug/rules" params={params}>
        Rules
      </Link>
      <Link to="/app/$organizationSlug/processes/$processSlug/versions" params={params}>
        Versions
      </Link>
    </span>
  );
}

export function Processes() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as { processStatus?: string };
  const statusFilter = STATUS_OPTIONS.some((option) => option.id === search.processStatus)
    ? (search.processStatus as string)
    : "all";

  const processes = useQuery({
    queryKey: ["processes", slug],
    queryFn: () => fetchProcesses(slug),
  });

  const rows = (processes.data ?? []).filter(
    (process) => statusFilter === "all" || process.status === statusFilter,
  );

  return (
    <AppShell
      title="Processes"
      breadcrumbs={[{ label: session.organization.name }, { label: "Processes" }]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
        {processes.status === "error" ? (
          <Banner
            tone="critical"
            title="Couldn’t load processes"
            action={
              <Button size="sm" onPress={() => void processes.refetch()}>
                Try again
              </Button>
            }
          >
            Nothing has been changed.
          </Banner>
        ) : null}

        <div style={{ maxWidth: "16rem" }}>
          <Select
            label="Status"
            items={STATUS_OPTIONS}
            selectedKey={statusFilter}
            onSelectionChange={(key) => {
              void navigate({
                to: ".",
                search: (previous: Record<string, unknown>) => ({
                  ...previous,
                  processStatus: key === "all" ? undefined : String(key),
                }),
              });
            }}
          />
        </div>

        <DataTable
          caption="Processes"
          columns={COLUMNS}
          data={rows}
          getRowId={(process) => process.id}
          status={
            processes.status === "pending"
              ? "loading"
              : processes.status === "error"
                ? "error"
                : "ready"
          }
          errorMessage="Couldn’t load processes. Nothing has been changed."
          onRetry={() => void processes.refetch()}
          emptyTitle="No processes yet"
          emptyBody="A process defines how one document type becomes an ERP-ready order. Create the first one to begin."
        />
      </div>
    </AppShell>
  );
}
