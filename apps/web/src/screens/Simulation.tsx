/**
 * Simulation screen (AIO-018): compares the CURRENT configuration's
 * evaluation against a CANDIDATE's. Critical (non-waivable) findings
 * render in an alert banner ABOVE the aggregates — averages never hide
 * a critical regression. Every chart-like bar is decorative
 * (aria-hidden) and sits inside an accessible table that carries the
 * same numbers.
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";

import {
  fetchStreamSimulation,
  type SimulationComparison,
  type SimulationRate,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const percent = (value: number | null) => (value === null ? "—" : `${(value * 100).toFixed(1)}%`);
const cents = (value: number) => `${(value / 100).toFixed(2)} €`;

function DeltaBadge({ delta, downIsGood = false }: { delta: number; downIsGood?: boolean }) {
  const improved = downIsGood ? delta < 0 : delta > 0;
  const tone = delta === 0 ? "neutral" : improved ? "success" : "critical";
  const sign = delta > 0 ? "+" : "";
  return <Badge tone={tone}>{`${sign}${(delta * 100).toFixed(1)} pp`}</Badge>;
}

function RateBar({ value }: { value: number | null }) {
  if (value === null) return null;
  return (
    <div
      aria-hidden="true"
      style={{ background: "var(--soa-border)", borderRadius: 3, height: 6, width: "8rem" }}
    >
      <div
        style={{
          background: "var(--soa-accent, #4a7)",
          borderRadius: 3,
          height: 6,
          width: `${Math.round(value * 100)}%`,
        }}
      />
    </div>
  );
}

function AggregateCard({
  label,
  rate,
  format,
  downIsGood = false,
}: {
  label: string;
  rate: SimulationRate;
  format: (value: number) => string;
  downIsGood?: boolean;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      style={{
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
        padding: "var(--soa-space-4)",
        display: "grid",
        gap: "0.25rem",
      }}
    >
      <span style={{ color: "var(--soa-text-muted)", fontSize: "0.85rem" }}>{label}</span>
      <strong style={{ fontSize: "1.1rem" }}>
        {format(rate.current)} → {format(rate.candidate)}
      </strong>
      <DeltaBadge delta={rate.candidate - rate.current} downIsGood={downIsGood} />
    </div>
  );
}

function ComparisonView({ comparison }: { comparison: SimulationComparison }) {
  const critical = comparison.findings.filter((finding) => !finding.waivable);
  const waivable = comparison.findings.filter((finding) => finding.waivable);
  return (
    <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
      {/* Critical findings come FIRST: an improved average never hides them. */}
      {critical.length > 0 ? (
        <Banner tone="critical" title="Critical regression — publication blocked">
          <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
            {critical.map((finding) => (
              <li key={finding.detail}>{finding.detail}</li>
            ))}
          </ul>
        </Banner>
      ) : null}
      {waivable.length > 0 ? (
        <Banner tone="warning" title="Regressions requiring a waiver">
          <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
            {waivable.map((finding) => (
              <li key={finding.detail}>{finding.detail}</li>
            ))}
          </ul>
        </Banner>
      ) : null}
      {comparison.findings.length === 0 ? (
        <Banner tone="success" title="No regressions found">
          The candidate meets or improves every gate.
        </Banner>
      ) : null}

      <section
        aria-label="Aggregate changes"
        style={{
          display: "grid",
          gap: "var(--soa-space-3)",
          gridTemplateColumns: "repeat(auto-fit, minmax(12rem, 1fr))",
        }}
      >
        <AggregateCard
          label="Field accuracy (exact)"
          rate={comparison.field_exact_rate}
          format={(value) => percent(value)}
        />
        <AggregateCard
          label="Field accuracy (normalized)"
          rate={comparison.field_normalized_rate}
          format={(value) => percent(value)}
        />
        <AggregateCard
          label="Review rate"
          rate={comparison.review_rate}
          format={(value) => percent(value)}
          downIsGood
        />
        <AggregateCard
          label="False auto-approvals"
          rate={comparison.false_auto_approval_rate}
          format={(value) => percent(value)}
          downIsGood
        />
        <AggregateCard
          label="Cost per run"
          rate={comparison.total_cost_cents}
          format={cents}
          downIsGood
        />
      </section>

      <section aria-label="Per-field accuracy">
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <caption style={{ textAlign: "left", fontWeight: 600, padding: "0.5rem 0" }}>
            Per-field exact accuracy — {comparison.current_label} vs {comparison.candidate_label}
          </caption>
          <thead>
            <tr>
              <th scope="col" style={{ textAlign: "left" }}>
                Field
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Current
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Candidate
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Change
              </th>
            </tr>
          </thead>
          <tbody>
            {comparison.field_diffs.map((diff) => (
              <tr key={diff.field} style={{ borderTop: "1px solid var(--soa-border)" }}>
                <th scope="row" style={{ textAlign: "left", fontWeight: 500 }}>
                  {diff.field}
                </th>
                <td>{percent(diff.current_exact_rate)}</td>
                <td style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                  {percent(diff.candidate_exact_rate)}
                  <RateBar value={diff.candidate_exact_rate} />
                </td>
                <td>{diff.delta === null ? "—" : <DeltaBadge delta={diff.delta} />}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section aria-label="Per-cohort accuracy">
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <caption style={{ textAlign: "left", fontWeight: 600, padding: "0.5rem 0" }}>
            Per-cohort exact accuracy
          </caption>
          <thead>
            <tr>
              <th scope="col" style={{ textAlign: "left" }}>
                Cohort
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Current
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Candidate
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Change
              </th>
            </tr>
          </thead>
          <tbody>
            {comparison.cohort_diffs.map((diff) => (
              <tr key={diff.cohort} style={{ borderTop: "1px solid var(--soa-border)" }}>
                <th scope="row" style={{ textAlign: "left", fontWeight: 500 }}>
                  {diff.cohort}
                </th>
                <td>{percent(diff.current_exact_rate)}</td>
                <td>{percent(diff.candidate_exact_rate)}</td>
                <td>{diff.delta === null ? "—" : <DeltaBadge delta={diff.delta} />}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section aria-label="Document drill-down">
        <h2 style={{ fontSize: "1rem" }}>Documents the candidate got wrong</h2>
        {comparison.documents.filter((doc) => doc.wrong_fields.length > 0).length === 0 ? (
          <p>Every gold document scored clean under the candidate.</p>
        ) : (
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}>
            {comparison.documents
              .filter((doc) => doc.wrong_fields.length > 0)
              .map((doc) => (
                <li
                  key={doc.document_sha256}
                  style={{
                    border: "1px solid var(--soa-border)",
                    borderRadius: "var(--soa-radius-panel)",
                    padding: "var(--soa-space-3)",
                  }}
                >
                  <details>
                    <summary style={{ cursor: "pointer" }}>
                      <code>{doc.document_sha256.slice(0, 12)}…</code>{" "}
                      <Badge tone="neutral">{doc.split}</Badge>{" "}
                      {doc.auto_approved ? (
                        <Badge tone="critical">would auto-approve despite errors</Badge>
                      ) : (
                        <Badge tone="warning">{`${doc.wrong_fields.length} wrong field(s)`}</Badge>
                      )}
                    </summary>
                    <p style={{ margin: "0.5rem 0 0" }}>
                      Wrong fields: {doc.wrong_fields.join(", ")}
                    </p>
                  </details>
                </li>
              ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export function Simulation() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const { streamSlug } = useParams({ strict: false }) as { streamSlug: string };
  const simulation = useQuery({
    queryKey: ["simulation", slug, streamSlug],
    queryFn: () => fetchStreamSimulation(slug, streamSlug),
  });

  return (
    <AppShell
      title="Simulation"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Streams", to: "/app/$organizationSlug/streams" },
        { label: streamSlug },
        { label: "Simulation" },
      ]}
    >
      {simulation.status === "pending" ? <Skeleton height="12rem" /> : null}
      {simulation.status === "error" ? (
        <Banner
          tone="critical"
          title="Couldn’t load the simulation"
          action={
            <Button size="sm" onPress={() => void simulation.refetch()}>
              Try again
            </Button>
          }
        >
          The service did not respond.
        </Banner>
      ) : null}
      {simulation.status === "success" && !simulation.data.available ? (
        <Banner tone="info" title="No evaluation runs yet">
          {simulation.data.reason}
        </Banner>
      ) : null}
      {simulation.status === "success" && simulation.data.available ? (
        <ComparisonView comparison={simulation.data.comparison} />
      ) : null}
    </AppShell>
  );
}
