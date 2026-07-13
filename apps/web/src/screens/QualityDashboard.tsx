/**
 * Quality dashboard (ANA-005): correction-proxy quality metrics with
 * their sample sizes and caveats IN the interface — every rate shows
 * its numerator/denominator, unmeasurable rates render as "not
 * measurable" (never 0), false auto-approval states plainly why
 * production cannot measure it, and the proxy nature of correction
 * rates is a banner, not a footnote.
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { CSSProperties, ReactNode } from "react";

import { fetchQualitySnapshot } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const panel: CSSProperties = {
  border: "1px solid var(--soa-border)",
  borderRadius: "var(--soa-radius-panel)",
  padding: "var(--soa-space-3)",
  display: "grid",
  gap: "0.375rem",
  color: "inherit",
  textDecoration: "none",
};

function rateLabel(rate: number | null): string {
  return rate === null ? "not measurable" : `${(rate * 100).toFixed(1)}%`;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} style={{ display: "grid", gap: "var(--soa-space-2)" }}>
      <h2 style={{ margin: 0 }}>{title}</h2>
      {children}
    </section>
  );
}

export function QualityDashboard() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const snapshot = useQuery({
    queryKey: ["quality", slug],
    queryFn: () => fetchQualitySnapshot(slug),
  });

  if (snapshot.status === "pending") {
    return (
      <AppShell title="Analytics" breadcrumbs={[{ label: session.organization.name }]}>
        <Skeleton height="24rem" />
      </AppShell>
    );
  }
  if (snapshot.status === "error") {
    return (
      <AppShell title="Analytics" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load the quality snapshot"
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
      title="Analytics"
      breadcrumbs={[{ label: session.organization.name }, { label: "Analytics" }]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
        <Banner tone="info" title="These are correction-based proxies, not accuracy">
          {data.ground_truth.note} Gold documents available: {data.ground_truth.gold_documents}.
        </Banner>
        <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
          Window: {windowLabel} · Sample: {data.reviewed.tasks_completed} review task(s) completed
          over {data.reviewed.runs} run(s)
        </p>

        <Section title="Field corrections">
          {data.field_corrections.length === 0 ? (
            <p style={{ margin: 0 }}>No reviewed runs in this window — nothing to measure.</p>
          ) : (
            <table style={{ borderCollapse: "collapse", maxWidth: "40rem" }}>
              <caption style={{ textAlign: "left", font: "var(--soa-font-caption)" }}>
                Corrected reviewed runs / reviewed runs where the field appeared
              </caption>
              <thead>
                <tr>
                  <th style={{ textAlign: "left" }}>Field</th>
                  <th style={{ textAlign: "right" }}>Corrected / present</th>
                  <th style={{ textAlign: "right" }}>Rate</th>
                </tr>
              </thead>
              <tbody>
                {data.field_corrections.map((entry) => (
                  <tr key={entry.field_key}>
                    <td>{entry.field_key}</td>
                    <td style={{ textAlign: "right" }}>
                      {entry.corrected_runs}/{entry.present_runs}
                    </td>
                    <td style={{ textAlign: "right" }}>{rateLabel(entry.correction_rate)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p style={{ margin: 0, font: "var(--soa-font-caption)" }}>
            Line cells: {data.line_corrections.cells_corrected}/
            {data.line_corrections.cells_present} corrected (
            {rateLabel(data.line_corrections.correction_rate)})
          </p>
          <Link
            to="/app/$organizationSlug/review"
            params={{ organizationSlug: slug }}
            style={{ font: "var(--soa-font-caption)" }}
          >
            Open the review queue
          </Link>
        </Section>

        <Section title="Straight-through processing">
          <div style={{ ...panel, maxWidth: "24rem" }}>
            <strong style={{ fontSize: "1.25rem" }}>
              {data.stp.straight_through}/{data.stp.settled_documents} settled without a human (
              {rateLabel(data.stp.stp_rate)})
            </strong>
            <span style={{ font: "var(--soa-font-caption)" }}>
              Documents received in the window, now settled.
            </span>
          </div>
        </Section>

        <Section title="False auto-approval">
          <Banner tone="warning" title="Not measurable from production data">
            {data.false_auto_approval.reason}
          </Banner>
        </Section>

        <Section title="Calibration">
          <table style={{ borderCollapse: "collapse", maxWidth: "40rem" }}>
            <caption style={{ textAlign: "left", font: "var(--soa-font-caption)" }}>
              Correction rate per extraction-confidence bucket (reviewed fields)
            </caption>
            <thead>
              <tr>
                <th style={{ textAlign: "left" }}>Confidence</th>
                <th style={{ textAlign: "right" }}>Corrected / reviewed</th>
                <th style={{ textAlign: "right" }}>Rate</th>
              </tr>
            </thead>
            <tbody>
              {data.calibration.cohorts.map((cohort) => (
                <tr key={cohort.confidence_range}>
                  <td>{cohort.confidence_range}</td>
                  <td style={{ textAlign: "right" }}>
                    {cohort.corrected}/{cohort.fields_reviewed}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {rateLabel(cohort.correction_rate)}{" "}
                    {cohort.fields_reviewed > 0 && cohort.fields_reviewed < 30 ? (
                      <Badge tone="warning">small sample</Badge>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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
                  {definition.denominator}.
                </dd>
              </div>
            ))}
          </dl>
        </details>
      </div>
    </AppShell>
  );
}
