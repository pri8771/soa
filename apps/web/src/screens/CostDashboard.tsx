/**
 * Cost dashboard (ANA-006): the ANA-003 usage ledger by stream /
 * provider / model / cost category. Estimated cost and the reconciled
 * (final) total are labeled as such and never merged; billed units are
 * provider FACTS shown per unit; providers appear by catalog name only
 * — no billing account identifiers or secrets exist on this surface.
 * Quota thresholds render only when a quota policy exists; until then
 * the absence is stated, not faked.
 */

import { Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { fetchUsageSnapshot } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

function dollars(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

export function CostDashboard() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const snapshot = useQuery({
    queryKey: ["usage", slug],
    queryFn: () => fetchUsageSnapshot(slug),
  });

  if (snapshot.status === "pending") {
    return (
      <AppShell title="Costs" breadcrumbs={[{ label: session.organization.name }]}>
        <Skeleton height="20rem" />
      </AppShell>
    );
  }
  if (snapshot.status === "error") {
    return (
      <AppShell title="Costs" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load the usage ledger"
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
  const windowLabel = `${data.window.since.slice(0, 10)} → ${data.window.until.slice(0, 10)} (${data.window.timezone})`;

  return (
    <AppShell
      title="Costs"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Analytics", to: "/app/$organizationSlug/analytics" },
        { label: "Costs" },
      ]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
        <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
          Window: {windowLabel}
        </p>

        {!data.quotas.configured ? (
          <Banner tone="info" title="No budget thresholds yet">
            {data.quotas.reason}
          </Banner>
        ) : null}

        <section aria-label="Totals" style={{ display: "flex", gap: "var(--soa-space-4)" }}>
          <div>
            <div style={{ font: "var(--soa-font-caption)" }}>Estimated</div>
            <strong style={{ fontSize: "1.5rem" }}>{dollars(data.totals.estimated_cents)}</strong>
          </div>
          <div>
            <div style={{ font: "var(--soa-font-caption)" }}>Adjustments</div>
            <strong style={{ fontSize: "1.5rem" }}>{dollars(data.totals.adjustment_cents)}</strong>
          </div>
          <div>
            <div style={{ font: "var(--soa-font-caption)" }}>Reconciled (final)</div>
            <strong style={{ fontSize: "1.5rem" }}>{dollars(data.totals.reconciled_cents)}</strong>
          </div>
        </section>

        <section aria-label="Usage by group" style={{ overflowX: "auto" }}>
          {data.groups.length === 0 ? (
            <p style={{ margin: 0 }}>No usage recorded in this window.</p>
          ) : (
            <table style={{ borderCollapse: "collapse", minWidth: "48rem" }}>
              <caption style={{ textAlign: "left", font: "var(--soa-font-caption)" }}>
                Usage by stream / provider / model / category. Billed quantities are
                provider-metered facts; estimated is our price estimate; reconciled = estimated +
                appended adjustments.
              </caption>
              <thead>
                <tr>
                  <th style={{ textAlign: "left" }}>Provider</th>
                  <th style={{ textAlign: "left" }}>Model</th>
                  <th style={{ textAlign: "left" }}>Category</th>
                  <th style={{ textAlign: "left" }}>Stream</th>
                  <th style={{ textAlign: "right" }}>Billed</th>
                  <th style={{ textAlign: "right" }}>Pages</th>
                  <th style={{ textAlign: "right" }}>Estimated</th>
                  <th style={{ textAlign: "right" }}>Reconciled (final)</th>
                </tr>
              </thead>
              <tbody>
                {data.groups.map((group, index) => (
                  <tr key={index}>
                    <td>{group.provider}</td>
                    <td>{group.provider_model ?? "—"}</td>
                    <td>{group.cost_category}</td>
                    <td>{group.stream_id ?? "—"}</td>
                    <td style={{ textAlign: "right" }}>
                      {group.billed_quantity !== null && group.billed_unit
                        ? `${group.billed_quantity} ${group.billed_unit}`
                        : "not metered"}
                    </td>
                    <td style={{ textAlign: "right" }}>{group.pages}</td>
                    <td style={{ textAlign: "right" }}>{dollars(group.estimated_cents)}</td>
                    <td style={{ textAlign: "right" }}>{dollars(group.reconciled_cents)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        <details>
          <summary style={{ cursor: "pointer" }}>What these values mean</summary>
          <dl style={{ margin: "var(--soa-space-2) 0 0" }}>
            {Object.entries(data.semantics).map(([key, meaning]) => (
              <div key={key}>
                <dt style={{ fontWeight: 600 }}>{key}</dt>
                <dd style={{ margin: 0 }}>{meaning}</dd>
              </div>
            ))}
          </dl>
        </details>

        <Link
          to="/app/$organizationSlug/analytics"
          params={{ organizationSlug: slug }}
          style={{ font: "var(--soa-font-caption)" }}
        >
          Back to quality analytics
        </Link>
      </div>
    </AppShell>
  );
}
