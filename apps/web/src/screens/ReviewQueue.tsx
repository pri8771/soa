/**
 * Review queue (REV-003, UI_UX_BLUEPRINT §5.5).
 *
 * The reviewer's worklist on the DataTable system: the required views
 * (all / mine / unassigned / overdue / blocked) and sort persisted in
 * the URL so queues are shareable deep links, reason summaries that say
 * WHY each document needs a human, SLA state, assignment, and claim /
 * release actions. "Start next" claims the highest-priority open task
 * atomically and shows the server's explanation of why that task is
 * next. Rows are keyboard-navigable (tab + Enter opens the document);
 * loading, error, and empty states are explicit.
 */

import { Badge, Banner, Button, Select } from "@soa/design-system";
import { useMutation, useQueryClient, useInfiniteQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import type { ColumnDef } from "@tanstack/react-table";

import {
  claimNextReviewTask,
  claimReviewTask,
  fetchReviewTasks,
  releaseReviewTask,
  type ReviewTaskEntry,
} from "../api/client";
import { DataTable } from "../components/table/DataTable";
import { AppShell } from "../shell/AppShell";
import { HelpTip } from "../components/help/HelpTip";
import { useShellSession } from "../shell/ShellContext";

const VIEW_OPTIONS = [
  { id: "all", label: "All active" },
  { id: "mine", label: "Mine" },
  { id: "unassigned", label: "Unassigned" },
  { id: "overdue", label: "Overdue (SLA)" },
  { id: "blocked", label: "Blocked" },
];

const SORT_OPTIONS = [
  { id: "priority", label: "Priority" },
  { id: "sla", label: "SLA due" },
  { id: "created", label: "Newest" },
];

function reasonSummary(task: ReviewTaskEntry): string {
  if (task.reasons.length === 0) return "—";
  const [first] = task.reasons;
  const source = first.field_key ?? first.rule_key ?? "";
  const head = `${first.code.replace(/_/g, " ")}${source ? `: ${source}` : ""}`;
  return task.reasons.length > 1 ? `${head} +${task.reasons.length - 1} more` : head;
}

function slaBadge(task: ReviewTaskEntry) {
  if (!task.sla_due_at) return <span>—</span>;
  const due = new Date(task.sla_due_at);
  const overdue = due.getTime() < Date.now();
  return (
    <Badge tone={overdue ? "critical" : "info"}>
      {overdue ? "overdue" : `due ${due.toLocaleString()}`}
    </Badge>
  );
}

export function ReviewQueue() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const me = `user:${session.userId}`;
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const search = useSearch({ strict: false }) as { view?: string; sort?: string };
  const view = VIEW_OPTIONS.some((o) => o.id === search.view) ? (search.view as string) : "all";
  const sort = SORT_OPTIONS.some((o) => o.id === search.sort)
    ? (search.sort as string)
    : "priority";

  const setUrlFilter = (key: string, value: string | undefined) => {
    void navigate({
      to: ".",
      search: (previous: Record<string, unknown>) => ({ ...previous, [key]: value }),
    });
  };

  const tasks = useInfiniteQuery({
    queryKey: ["review-tasks", slug, view, sort],
    queryFn: ({ pageParam }) =>
      fetchReviewTasks(slug, { view, sort, cursor: pageParam ?? undefined }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor : null),
  });
  const rows = tasks.data?.pages.flatMap((page) => page.items) ?? [];

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["review-tasks", slug] });
  const claim = useMutation({
    mutationFn: (id: string) => claimReviewTask(slug, id),
    onSettled: invalidate,
  });
  const release = useMutation({
    mutationFn: (id: string) => releaseReviewTask(slug, id),
    onSettled: invalidate,
  });
  const startNext = useMutation({
    mutationFn: () => claimNextReviewTask(slug),
    onSettled: invalidate,
  });

  const openTask = (task: ReviewTaskEntry) =>
    void navigate({
      to: "/app/$organizationSlug/review/$taskId",
      params: { organizationSlug: slug, taskId: task.id },
    });

  const columns: ColumnDef<ReviewTaskEntry>[] = [
    {
      header: "Document",
      cell: ({ row }) => row.original.document_filename ?? row.original.document_id.slice(0, 8),
    },
    {
      header: "Why it needs review",
      cell: ({ row }) => reasonSummary(row.original),
    },
    {
      header: "Priority",
      cell: ({ row }) => (
        <span style={{ display: "inline-flex", gap: "var(--soa-space-1)" }}>
          {row.original.priority}
          {row.original.blocking ? <Badge tone="critical">blocked</Badge> : null}
        </span>
      ),
    },
    { header: "SLA", cell: ({ row }) => slaBadge(row.original) },
    {
      header: "Assigned",
      cell: ({ row }) =>
        row.original.assigned_to === me
          ? "me"
          : row.original.assigned_to
            ? row.original.assigned_to
            : "—",
    },
    {
      header: "Actions",
      cell: ({ row }) =>
        row.original.state === "open" ? (
          <Button
            size="sm"
            variant="subtle"
            aria-label={`Claim ${row.original.document_filename ?? row.original.id}`}
            onPress={() => claim.mutate(row.original.id)}
          >
            Claim
          </Button>
        ) : row.original.assigned_to === me ? (
          <Button
            size="sm"
            variant="subtle"
            aria-label={`Release ${row.original.document_filename ?? row.original.id}`}
            onPress={() => release.mutate(row.original.id)}
          >
            Release
          </Button>
        ) : null,
    },
  ];

  return (
    <AppShell
      title="Review"
      breadcrumbs={[{ label: session.organization.name }, { label: "Review" }]}
      actions={
        <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
          <HelpTip topic="review" />
          <Button onPress={() => startNext.mutate()} isDisabled={startNext.isPending}>
            Start next
          </Button>
        </div>
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
        {claim.isError ? (
          <Banner tone="critical" title="Claim failed">
            {claim.error?.message ?? "The task was not claimed."}
          </Banner>
        ) : null}
        {startNext.isError ? (
          <Banner tone="critical" title="Couldn’t start the next task">
            {startNext.error?.message ?? "Nothing was claimed."}
          </Banner>
        ) : null}
        {startNext.isSuccess && startNext.data.task ? (
          <Banner
            tone="success"
            title={`Claimed ${startNext.data.task.document_filename ?? "task"}`}
          >
            {startNext.data.explanation}{" "}
            <Button
              size="sm"
              variant="subtle"
              onPress={() => startNext.data.task && openTask(startNext.data.task)}
            >
              Open in Review Studio
            </Button>
          </Banner>
        ) : null}
        {startNext.isSuccess && !startNext.data.task ? (
          <Banner tone="info" title="Queue is clear">
            {startNext.data.explanation}
          </Banner>
        ) : null}

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
              label="View"
              items={VIEW_OPTIONS}
              selectedKey={view}
              onSelectionChange={(key) =>
                setUrlFilter("view", key === "all" ? undefined : String(key))
              }
            />
          </div>
          <div style={{ minWidth: "10rem" }}>
            <Select
              label="Sort"
              items={SORT_OPTIONS}
              selectedKey={sort}
              onSelectionChange={(key) =>
                setUrlFilter("sort", key === "priority" ? undefined : String(key))
              }
            />
          </div>
        </div>

        <DataTable
          caption="Review tasks"
          columns={columns}
          data={rows}
          getRowId={(row) => row.id}
          status={
            tasks.status === "pending" ? "loading" : tasks.status === "error" ? "error" : "ready"
          }
          errorMessage="Couldn’t load the review queue. Nothing has been changed."
          onRetry={() => void tasks.refetch()}
          emptyTitle="No review tasks"
          emptyBody="Nothing in this view needs a human right now."
          hasMore={tasks.hasNextPage}
          onLoadMore={() => void tasks.fetchNextPage()}
          onRowActivate={openTask}
        />
      </div>
    </AppShell>
  );
}
