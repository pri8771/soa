/**
 * Documents queue (ING-010, UI_UX_BLUEPRINT §5.3).
 *
 * The tenant's document backlog on the DataTable system: documented
 * columns (name, stream, state, channel, size, received), state and
 * channel filters plus filename search persisted in the URL so views
 * are shareable, cursor-based "load more", and selection with the one
 * bulk action that is currently valid server-side — cancel — offered
 * only when every selected document is in a cancellable state. Row
 * states are distinct: duplicates show a flag even while queued, and
 * exceptional states carry their safe reason inline.
 */

import { Badge, Button, Select, TextField } from "@soa/design-system";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { useState } from "react";

import { cancelDocument, fetchDocuments, fetchStreams, type DocumentSummary } from "../api/client";
import { DataTable } from "../components/table/DataTable";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const STATE_OPTIONS = [
  { id: "all", label: "All states" },
  { id: "received", label: "Received" },
  { id: "queued", label: "Queued" },
  { id: "review_required", label: "Review required" },
  { id: "completed", label: "Completed" },
  { id: "rejected", label: "Rejected" },
  { id: "quarantined", label: "Quarantined" },
  { id: "failed_retryable", label: "Failed (retryable)" },
  { id: "failed_terminal", label: "Failed (terminal)" },
  { id: "cancelled", label: "Cancelled" },
];

const CHANNEL_OPTIONS = [
  { id: "all", label: "All channels" },
  { id: "upload", label: "Upload" },
  { id: "api", label: "API" },
  { id: "email", label: "Email" },
];

const STATE_TONES: Record<
  string,
  "neutral" | "accent" | "success" | "warning" | "critical" | "info"
> = {
  received: "neutral",
  validating_file: "accent",
  queued: "info",
  review_required: "warning",
  approved: "success",
  completed: "success",
  rejected: "critical",
  quarantined: "critical",
  failed_retryable: "warning",
  failed_terminal: "critical",
  cancelled: "neutral",
  archived: "neutral",
};

//: States the server's transition map allows to cancel.
const CANCELLABLE_STATES = new Set([
  "received",
  "validating_file",
  "queued",
  "preprocessing",
  "classifying",
  "splitting",
  "extracting",
  "normalizing",
  "validating_data",
  "review_required",
  "exporting",
]);

function formatSize(bytes: number): string {
  if (bytes >= 1_048_576) return `${(bytes / 1_048_576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

export function DocumentsQueue() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canReview = session.permissions.has("documents.review");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const search = useSearch({ strict: false }) as {
    docState?: string;
    docChannel?: string;
    docSearch?: string;
  };
  const stateFilter = STATE_OPTIONS.some((o) => o.id === search.docState)
    ? (search.docState as string)
    : "all";
  const channelFilter = CHANNEL_OPTIONS.some((o) => o.id === search.docChannel)
    ? (search.docChannel as string)
    : "all";
  const searchText = search.docSearch ?? "";
  const [searchDraft, setSearchDraft] = useState(searchText);

  const setUrlFilter = (key: string, value: string | undefined) => {
    void navigate({
      to: ".",
      search: (previous: Record<string, unknown>) => ({ ...previous, [key]: value }),
    });
  };

  const streams = useQuery({ queryKey: ["streams", slug], queryFn: () => fetchStreams(slug) });
  const streamNames = new Map((streams.data ?? []).map((s) => [s.id, s.name]));

  const documents = useInfiniteQuery({
    queryKey: ["documents", slug, stateFilter, channelFilter, searchText],
    queryFn: ({ pageParam }) =>
      fetchDocuments(slug, {
        state: stateFilter === "all" ? undefined : stateFilter,
        channel: channelFilter === "all" ? undefined : channelFilter,
        search: searchText || undefined,
        cursor: pageParam ?? undefined,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : null),
  });
  const rows = documents.data?.pages.flatMap((page) => page.items) ?? [];

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const selectedDocs = rows.filter((row) => selected.has(row.id));
  const allCancellable =
    selectedDocs.length > 0 && selectedDocs.every((d) => CANCELLABLE_STATES.has(d.state));

  const cancel = useMutation({
    mutationFn: async (ids: string[]) => {
      for (const id of ids) {
        await cancelDocument(slug, id, "cancelled from the documents queue");
      }
    },
    onSettled: () => {
      setSelected(new Set());
      void queryClient.invalidateQueries({ queryKey: ["documents", slug] });
    },
  });

  const columns: ColumnDef<DocumentSummary>[] = [
    { header: "Document", accessorKey: "original_filename" },
    {
      header: "Stream",
      cell: ({ row }) => streamNames.get(row.original.stream_id) ?? "…",
    },
    {
      header: "State",
      cell: ({ row }) => (
        <span style={{ display: "inline-flex", gap: "var(--soa-space-1)", alignItems: "center" }}>
          <Badge tone={STATE_TONES[row.original.state] ?? "neutral"}>
            {row.original.state.replace(/_/g, " ")}
          </Badge>
          {row.original.duplicate_of ? <Badge tone="warning">duplicate</Badge> : null}
        </span>
      ),
    },
    {
      header: "Reason",
      cell: ({ row }) => row.original.state_reason ?? "—",
    },
    { header: "Channel", accessorKey: "source_channel" },
    {
      header: "Size",
      cell: ({ row }) => formatSize(row.original.size_bytes),
    },
    {
      header: "Received",
      cell: ({ row }) => new Date(row.original.received_at).toLocaleString(),
    },
  ];

  return (
    <AppShell
      title="Documents"
      breadcrumbs={[{ label: session.organization.name }, { label: "Documents" }]}
      actions={
        <Link to="/app/$organizationSlug/documents/upload" params={{ organizationSlug: slug }}>
          Upload documents
        </Link>
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
        <div
          style={{
            display: "flex",
            gap: "var(--soa-space-3)",
            alignItems: "end",
            flexWrap: "wrap",
          }}
        >
          <div style={{ minWidth: "12rem" }}>
            <Select
              label="State"
              items={STATE_OPTIONS}
              selectedKey={stateFilter}
              onSelectionChange={(key) =>
                setUrlFilter("docState", key === "all" ? undefined : String(key))
              }
            />
          </div>
          <div style={{ minWidth: "10rem" }}>
            <Select
              label="Channel"
              items={CHANNEL_OPTIONS}
              selectedKey={channelFilter}
              onSelectionChange={(key) =>
                setUrlFilter("docChannel", key === "all" ? undefined : String(key))
              }
            />
          </div>
          <TextField label="Search filename" value={searchDraft} onChange={setSearchDraft} />
          <Button
            variant="secondary"
            onPress={() => setUrlFilter("docSearch", searchDraft || undefined)}
          >
            Search
          </Button>
        </div>

        <DataTable
          caption="Documents"
          columns={columns}
          data={rows}
          getRowId={(row) => row.id}
          status={
            documents.status === "pending"
              ? "loading"
              : documents.status === "error"
                ? "error"
                : "ready"
          }
          errorMessage="Couldn’t load documents. Nothing has been changed."
          onRetry={() => void documents.refetch()}
          emptyTitle="No documents"
          emptyBody="Nothing matches the current filters. Upload documents or adjust the filters."
          selectedIds={canReview ? selected : undefined}
          onSelectionChange={canReview ? setSelected : undefined}
          bulkActions={
            canReview ? (
              <Button
                size="sm"
                variant="destructive"
                isDisabled={!allCancellable || cancel.isPending}
                onPress={() => cancel.mutate([...selected])}
              >
                Cancel selected
              </Button>
            ) : undefined
          }
          hasMore={documents.hasNextPage}
          onLoadMore={() => void documents.fetchNextPage()}
          onRowActivate={(row) =>
            void navigate({
              to: "/app/$organizationSlug/documents/$documentId",
              params: { organizationSlug: slug, documentId: row.id },
            })
          }
        />
        {canReview && selectedDocs.length > 0 && !allCancellable ? (
          <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
            Cancel is unavailable: the selection includes documents that are already settled.
          </p>
        ) : null}
      </div>
    </AppShell>
  );
}
