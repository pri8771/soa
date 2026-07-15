/**
 * Operations dashboard (ANA-004): what needs attention right now, the
 * windowed operational metrics, and exceptions — every tile links to
 * the filtered operational view that shows the underlying items, and
 * every number's definition (numerator/denominator/timezone) is one
 * disclosure away. No decorative metrics: if a number has no
 * definition and no destination, it is not on this screen.
 */

import { Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { CSSProperties, ReactNode } from "react";

import { fetchOperationsSnapshot, type AttentionItem } from "../api/client";
import { DASHBOARD_POLL_MS, liveQueryOptions } from "../app/liveQuery";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const tile: CSSProperties = {
  border: "1px solid var(--soa-border)",
  borderRadius: "var(--soa-radius-panel)",
  padding: "var(--soa-space-3)",
  display: "grid",
  gap: "0.375rem",
  textDecoration: "none",
  color: "inherit",
};

/** Map the API's declarative drill-down target onto client routes. */
function attentionTarget(item: AttentionItem): { to: string; search?: Record<string, string> } {
  switch (item.link.screen) {
    case "review":
      return { to: "/app/$organizationSlug/review", search: item.link.filters };
    case "documents":
      return { to: "/app/$organizationSlug/documents", search: item.link.filters };
    case "integrations":
      return { to: "/app/$organizationSlug/integrations" };
    default:
      return { to: "/app/$organizationSlug/documents" };
  }
}

function AttentionTile({ item, slug }: { item: AttentionItem; slug: string }) {
  const target = attentionTarget(item);
  return (
    <Link
      to={target.to}
      params={{ organizationSlug: slug }}
      search={target.search}
      style={tile}
      aria-label={`${item.label}: ${item.count} — open the filtered view`}
    >
      <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
        {item.label}
      </span>
      <strong style={{ fontSize: "1.5rem" }}>{item.count}</strong>
    </Link>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} style={{ display: "grid", gap: "var(--soa-space-2)" }}>
      <h2 style={{ margin: 0, font: "var(--soa-font-heading-sm, inherit)" }}>{title}</h2>
      {children}
    </section>
  );
}

function CountsTable({ caption, counts }: { caption: string; counts: Record<string, number> }) {
  const rows = Object.entries(counts);
  if (rows.length === 0) return <p style={{ margin: 0 }}>Nothing in this window.</p>;
  return (
    <table style={{ borderCollapse: "collapse" }}>
      <caption style={{ textAlign: "left", font: "var(--soa-font-caption)" }}>{caption}</caption>
      <tbody>
        {rows.map(([key, value]) => (
          <tr key={key}>
            <th scope="row" style={{ textAlign: "left", paddingRight: "1rem", fontWeight: 400 }}>
              {key.replace(/_/g, " ")}
            </th>
            <td style={{ textAlign: "right" }}>{value}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Operations() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const snapshot = useQuery({
    queryKey: ["operations", slug],
    queryFn: () => fetchOperationsSnapshot(slug),
    ...liveQueryOptions(DASHBOARD_POLL_MS),
  });

  if (snapshot.status === "pending") {
    return (
      <AppShell title="Overview" breadcrumbs={[{ label: session.organization.name }]}>
        <Skeleton height="24rem" />
      </AppShell>
    );
  }
  if (snapshot.status === "error") {
    return (
      <AppShell title="Overview" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load the operations snapshot"
          action={
            <Button size="sm" onPress={() => void snapshot.refetch()}>
              Try again
            </Button>
          }
        >
          The analytics service did not respond.
        </Banner>
      </AppShell>
    );
  }

  const data = snapshot.data;
  const received = data.volume.per_day.reduce((sum, entry) => sum + entry.count, 0);
  const perDay = new Map<string, number>();
  for (const entry of data.volume.per_day) {
    perDay.set(entry.day, (perDay.get(entry.day) ?? 0) + entry.count);
  }
  const windowLabel = `${data.window.since.slice(0, 10)} → ${data.window.until.slice(0, 10)} (${data.window.timezone})`;

  return (
    <AppShell
      title="Overview"
      breadcrumbs={[{ label: session.organization.name }, { label: "Overview" }]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
        <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
          Window: {windowLabel}
        </p>

        <Section title="Needs attention">
          <ul
            style={{
              listStyle: "none",
              margin: 0,
              padding: 0,
              display: "grid",
              gap: "var(--soa-space-2)",
              gridTemplateColumns: "repeat(auto-fill, minmax(14rem, 1fr))",
            }}
          >
            {data.needs_attention.map((item) => (
              <li key={item.key} style={{ display: "grid" }}>
                <AttentionTile item={item} slug={slug} />
              </li>
            ))}
            <li style={{ display: "grid" }}>
              <Link
                to="/app/$organizationSlug/jobs"
                params={{ organizationSlug: slug }}
                style={tile}
                aria-label="Job queue health — open the jobs queue"
              >
                <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                  Job queue health
                </span>
                <strong>Open the jobs queue</strong>
              </Link>
            </li>
            <li style={{ display: "grid" }}>
              <Link
                to="/app/$organizationSlug/audit"
                params={{ organizationSlug: slug }}
                style={tile}
                aria-label="Audit trail — open the filtered audit query"
              >
                <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                  Audit trail
                </span>
                <strong>Query and export</strong>
              </Link>
            </li>
          </ul>
        </Section>

        <Section title="Processing">
          <div style={{ display: "flex", gap: "var(--soa-space-4)", flexWrap: "wrap" }}>
            <Link
              to="/app/$organizationSlug/documents"
              params={{ organizationSlug: slug }}
              style={tile}
              aria-label={`Documents received in the window: ${received} — open the documents queue`}
            >
              <span style={{ font: "var(--soa-font-caption)" }}>Received in window</span>
              <strong style={{ fontSize: "1.5rem" }}>{received}</strong>
            </Link>
            <div style={tile} aria-label="Run latency">
              <span style={{ font: "var(--soa-font-caption)" }}>
                Run latency ({data.latency.runs_measured} runs measured
                {data.latency.sample_capped ? ", sample capped" : ""})
              </span>
              <strong>
                {data.latency.runs_measured === 0
                  ? "no finished runs in the window"
                  : `p50 ${data.latency.p50_ms} ms · p95 ${data.latency.p95_ms} ms · avg ${data.latency.avg_ms} ms`}
              </strong>
            </div>
            <div style={tile} aria-label="SLA">
              <span style={{ font: "var(--soa-font-caption)" }}>SLA</span>
              <strong>
                {data.sla.overdue_now}/{data.sla.active_with_sla} active overdue ·{" "}
                {data.sla.breached_completed}/{data.sla.completed_with_sla} completed breached
              </strong>
            </div>
          </div>
          <CountsTable
            caption="Documents received per UTC day"
            counts={Object.fromEntries(perDay)}
          />
        </Section>

        <Section title="Backlog">
          <div style={{ display: "flex", gap: "var(--soa-space-4)", flexWrap: "wrap" }}>
            <Link
              to="/app/$organizationSlug/documents"
              params={{ organizationSlug: slug }}
              style={tile}
              aria-label="Documents in the pipeline — open the documents queue"
            >
              <CountsTable
                caption="Documents in the pipeline (now)"
                counts={data.backlog.documents_by_state}
              />
            </Link>
            <Link
              to="/app/$organizationSlug/review"
              params={{ organizationSlug: slug }}
              style={tile}
              aria-label="Review tasks — open the review queue"
            >
              <CountsTable caption="Review tasks (now)" counts={data.backlog.review_tasks} />
            </Link>
          </div>
        </Section>

        <Section title="Exceptions">
          <Link
            to="/app/$organizationSlug/documents"
            params={{ organizationSlug: slug }}
            search={{ docState: "quarantined" }}
            style={{ ...tile, maxWidth: "24rem" }}
            aria-label="Documents in exceptional states — open the documents queue"
          >
            <CountsTable
              caption="Documents in exceptional states (now)"
              counts={data.exceptions.documents_by_state}
            />
          </Link>
        </Section>

        <Section title="Exports">
          <Link
            to="/app/$organizationSlug/integrations"
            params={{ organizationSlug: slug }}
            style={{ ...tile, maxWidth: "24rem" }}
            aria-label="Export jobs — open integrations"
          >
            <CountsTable caption="Export jobs in the window" counts={data.exports.jobs_by_state} />
            <span style={{ font: "var(--soa-font-caption)" }}>
              Delivery attempts: {data.exports.attempts_delivered}/{data.exports.attempts_total}{" "}
              delivered
            </span>
          </Link>
        </Section>

        {data.notes.length > 0 ? (
          <Banner tone="info" title="Notes on these numbers">
            <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
              {data.notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </Banner>
        ) : null}

        <details>
          <summary style={{ cursor: "pointer" }}>What these numbers mean</summary>
          <dl style={{ margin: "var(--soa-space-2) 0 0" }}>
            {Object.values(data.definitions).map((definition) => (
              <div key={definition.key} style={{ marginBottom: "var(--soa-space-2)" }}>
                <dt style={{ fontWeight: 600 }}>{definition.key}</dt>
                <dd style={{ margin: 0 }}>
                  {definition.description} Numerator: {definition.numerator}. Denominator:{" "}
                  {definition.denominator}. Timezone: {definition.timezone}.
                </dd>
              </div>
            ))}
          </dl>
        </details>
      </div>
    </AppShell>
  );
}
