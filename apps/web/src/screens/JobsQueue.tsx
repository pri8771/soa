/**
 * Queue health screen (JOB-007, UI_UX_BLUEPRINT §6.3).
 *
 * Shows tenant queue depth, oldest pending age, a status-filtered job
 * table, and audited replay/cancel controls. Failure reasons come from the
 * server's safe error summary; payloads are never fetched or shown. The
 * status filter lives in the URL, so the periodic refresh cannot reset it.
 */

import {
  Badge,
  Banner,
  Button,
  Dialog,
  DialogTrigger,
  Select,
  TextField,
} from "@soa/design-system";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";
import { useState } from "react";

import { cancelJob, fetchJobs, fetchJobStats, replayJob, type JobSummary } from "../api/client";
import { liveQueryOptions, QUEUE_POLL_MS } from "../app/liveQuery";
import { DataTable } from "../components/table/DataTable";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const STATUS_TONES: Record<
  JobSummary["status"],
  "neutral" | "accent" | "success" | "warning" | "critical"
> = {
  pending: "neutral",
  running: "accent",
  succeeded: "success",
  dead_letter: "critical",
  cancelled: "warning",
};

const STATUS_OPTIONS = [
  { id: "all", label: "All statuses" },
  { id: "pending", label: "Pending" },
  { id: "running", label: "Running" },
  { id: "succeeded", label: "Succeeded" },
  { id: "dead_letter", label: "Dead letter" },
  { id: "cancelled", label: "Cancelled" },
];

function formatAge(iso: string | null): string {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0) return "scheduled";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "<1 min";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.floor(hours / 24)} d`;
}

function StatCard({ label, value, tone }: { label: string; value: string; tone?: "critical" }) {
  return (
    <div
      style={{
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
        background: "var(--soa-surface)",
        padding: "var(--soa-space-4)",
        minWidth: "9rem",
      }}
    >
      <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-secondary)" }}>
        {label}
      </p>
      <p
        style={{
          margin: 0,
          font: "var(--soa-font-heading-lg)",
          color: tone === "critical" && value !== "0" ? "var(--soa-critical)" : undefined,
        }}
      >
        {value}
      </p>
    </div>
  );
}

function ReasonDialog({
  title,
  actionLabel,
  onConfirm,
}: {
  title: string;
  actionLabel: string;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <Dialog title={title}>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <TextField
            label="Reason (recorded in the audit log)"
            value={reason}
            onChange={setReason}
            isRequired
          />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Keep as is
            </Button>
            <Button
              isDisabled={reason.trim().length < 3}
              onPress={() => {
                onConfirm(reason.trim());
                close();
              }}
            >
              {actionLabel}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

export function JobsQueue() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("jobs.manage");
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as { jobStatus?: string };
  // Shared URLs may carry anything; unknown values fall back to "all"
  // instead of confusing the Select and the API.
  const statusFilter = STATUS_OPTIONS.some((option) => option.id === search.jobStatus)
    ? (search.jobStatus as string)
    : "all";

  const stats = useQuery({
    queryKey: ["jobs-stats", slug],
    queryFn: () => fetchJobStats(slug),
    ...liveQueryOptions(QUEUE_POLL_MS),
  });
  const jobs = useInfiniteQuery({
    queryKey: ["jobs", slug, statusFilter],
    queryFn: ({ pageParam }) =>
      fetchJobs(slug, {
        status: statusFilter === "all" ? undefined : statusFilter,
        cursor: pageParam ?? undefined,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : null),
    ...liveQueryOptions(QUEUE_POLL_MS),
  });
  const jobItems = jobs.data?.pages.flatMap((page) => page.items) ?? [];

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["jobs", slug] });
    void queryClient.invalidateQueries({ queryKey: ["jobs-stats", slug] });
  };
  const replay = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => replayJob(slug, id, reason),
    onSettled: invalidate,
  });
  const cancel = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) => cancelJob(slug, id, reason),
    onSettled: invalidate,
  });

  const columns: ColumnDef<JobSummary>[] = [
    { header: "Type", accessorKey: "job_type" },
    {
      header: "Status",
      accessorKey: "status",
      cell: ({ row }) => (
        <Badge tone={STATUS_TONES[row.original.status]}>
          {row.original.status.replace("_", " ")}
        </Badge>
      ),
    },
    {
      header: "Attempts",
      cell: ({ row }) => `${row.original.attempts}/${row.original.max_attempts}`,
    },
    {
      header: "Age",
      cell: ({ row }) => formatAge(row.original.created_at),
    },
    {
      header: "Failure reason",
      cell: ({ row }) => row.original.last_error ?? "—",
    },
    ...(canManage
      ? [
          {
            id: "actions",
            header: "Actions",
            cell: ({ row }: { row: { original: JobSummary } }) => {
              const job = row.original;
              if (job.status === "dead_letter") {
                return (
                  <DialogTrigger>
                    <Button size="sm" variant="secondary">
                      Replay
                    </Button>
                    <ReasonDialog
                      title={`Replay ${job.job_type}`}
                      actionLabel="Replay job"
                      onConfirm={(reason) => replay.mutate({ id: job.id, reason })}
                    />
                  </DialogTrigger>
                );
              }
              if (job.status === "pending") {
                return (
                  <DialogTrigger>
                    <Button size="sm" variant="subtle">
                      Cancel
                    </Button>
                    <ReasonDialog
                      title={`Cancel ${job.job_type}`}
                      actionLabel="Cancel job"
                      onConfirm={(reason) => cancel.mutate({ id: job.id, reason })}
                    />
                  </DialogTrigger>
                );
              }
              return null;
            },
          } satisfies ColumnDef<JobSummary>,
        ]
      : []),
  ];

  const byStatus = stats.data?.by_status ?? {};
  const depth = (byStatus["pending"] ?? 0) + (byStatus["running"] ?? 0);

  return (
    <AppShell title="Jobs" breadcrumbs={[{ label: session.organization.name }, { label: "Jobs" }]}>
      <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
        {replay.isError || cancel.isError ? (
          <Banner tone="critical" title="Action failed">
            {(replay.error ?? cancel.error)?.message ?? "The job was not changed."}
          </Banner>
        ) : null}

        {stats.status === "error" ? (
          // Honest failure: never render confident zeros off a failed query.
          <Banner
            tone="critical"
            title="Couldn’t load queue statistics"
            action={
              <Button size="sm" onPress={() => void stats.refetch()}>
                Try again
              </Button>
            }
          >
            The job list below may still be current.
          </Banner>
        ) : (
          <div style={{ display: "flex", gap: "var(--soa-space-4)", flexWrap: "wrap" }}>
            <StatCard label="Queue depth" value={stats.data ? String(depth) : "…"} />
            <StatCard
              label="Oldest pending"
              value={stats.data ? formatAge(stats.data.oldest_pending_run_after) : "…"}
            />
            <StatCard
              label="Dead letter"
              value={stats.data ? String(byStatus["dead_letter"] ?? 0) : "…"}
              tone="critical"
            />
            <StatCard
              label="Succeeded"
              value={stats.data ? String(byStatus["succeeded"] ?? 0) : "…"}
            />
          </div>
        )}

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
                  jobStatus: key === "all" ? undefined : String(key),
                }),
              });
            }}
          />
        </div>

        <DataTable
          caption="Jobs"
          columns={columns}
          data={jobItems}
          getRowId={(job) => job.id}
          status={
            jobs.status === "pending" ? "loading" : jobs.status === "error" ? "error" : "ready"
          }
          errorMessage="Couldn’t load jobs. Nothing has been changed."
          onRetry={() => void jobs.refetch()}
          emptyTitle="No jobs"
          emptyBody="Jobs appear here as documents are processed."
          hasMore={jobs.hasNextPage}
          onLoadMore={() => void jobs.fetchNextPage()}
        />
      </div>
    </AppShell>
  );
}
