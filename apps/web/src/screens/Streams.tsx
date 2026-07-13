/**
 * Streams list (CFG-010): every intake across the organization's
 * processes, with the parent process always visible for hierarchy.
 */

import { Badge, Banner, Button } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";

import { fetchStreams, type StreamSummary } from "../api/client";
import { DataTable } from "../components/table/DataTable";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const STATUS_TONES = {
  active: "success",
  paused: "warning",
  archived: "neutral",
} as const;

export function Streams() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const streams = useQuery({ queryKey: ["streams", slug], queryFn: () => fetchStreams(slug) });

  const columns: ColumnDef<StreamSummary>[] = [
    {
      header: "Stream",
      cell: ({ row }) => (
        <Link
          to="/app/$organizationSlug/streams/$streamSlug"
          params={{ organizationSlug: slug, streamSlug: row.original.slug }}
        >
          {row.original.name}
        </Link>
      ),
    },
    { header: "Process", accessorKey: "process_name" },
    {
      header: "Status",
      cell: ({ row }) => (
        <Badge tone={STATUS_TONES[row.original.status]}>{row.original.status}</Badge>
      ),
    },
    {
      header: "Published version",
      cell: ({ row }) =>
        row.original.active_version_number !== null ? (
          `v${row.original.active_version_number}`
        ) : (
          <Badge tone="warning">never published</Badge>
        ),
    },
  ];

  return (
    <AppShell
      title="Streams"
      breadcrumbs={[{ label: session.organization.name }, { label: "Streams" }]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
        {streams.status === "error" ? (
          <Banner
            tone="critical"
            title="Couldn’t load streams"
            action={
              <Button size="sm" onPress={() => void streams.refetch()}>
                Try again
              </Button>
            }
          >
            Nothing has been changed.
          </Banner>
        ) : null}
        <DataTable
          caption="Streams"
          columns={columns}
          data={streams.data ?? []}
          getRowId={(stream) => stream.id}
          status={
            streams.status === "pending"
              ? "loading"
              : streams.status === "error"
                ? "error"
                : "ready"
          }
          errorMessage="Couldn’t load streams. Nothing has been changed."
          onRetry={() => void streams.refetch()}
          emptyTitle="No streams yet"
          emptyBody="A stream is one intake of documents into a process — create one from a process page."
        />
      </div>
    </AppShell>
  );
}
