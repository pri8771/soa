/**
 * Skills home — the landing screen (Blueprint IA).
 *
 * A skill is a stream: the trained, versioned extraction capability documents
 * are routed to. Skills are grouped by their parent process — the bucket
 * grouping until classifier-driven intakes land — and each card carries the
 * numbers an operator triages by: queue depth, 30-day volume, measured
 * accuracy, training state. Selecting a skill opens its dashboard.
 */

import { Badge, Banner, Button } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";

import { fetchSkillsOverview, type SkillSummary } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

function accuracyLabel(skill: SkillSummary): string {
  if (skill.field_accuracy === null || skill.field_accuracy === undefined) return "—";
  return `${(skill.field_accuracy * 100).toFixed(1)}%`;
}

export function SkillsHome() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const navigate = useNavigate();
  const overview = useQuery({
    queryKey: ["skills", slug],
    queryFn: () => fetchSkillsOverview(slug),
  });

  const items = overview.data?.items ?? [];
  const groups = new Map<string, SkillSummary[]>();
  for (const skill of items) {
    const key = skill.process_name ?? "Ungrouped";
    groups.set(key, [...(groups.get(key) ?? []), skill]);
  }
  const totalInReview = items.reduce((acc, skill) => acc + skill.in_review, 0);

  return (
    <AppShell
      title="Skills"
      breadcrumbs={[{ label: session.organization.name }, { label: "Skills" }]}
      actions={
        <div style={{ display: "flex", gap: "var(--soa-space-2)" }}>
          <Button
            variant="secondary"
            onPress={() =>
              void navigate({
                to: "/app/$organizationSlug/documents/upload",
                params: { organizationSlug: slug },
              })
            }
          >
            Upload
          </Button>
          <Button
            onPress={() =>
              void navigate({
                to: "/app/$organizationSlug/review",
                params: { organizationSlug: slug },
              })
            }
          >
            Review queue · {totalInReview}
          </Button>
        </div>
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-6)", maxWidth: "72rem" }}>
        {overview.status === "error" ? (
          <Banner tone="critical" title="Couldn’t load skills">
            Try again.
          </Banner>
        ) : null}

        {[...groups.entries()].map(([processName, skills]) => (
          <section key={processName} style={{ display: "grid", gap: "var(--soa-space-3)" }}>
            <div
              style={{
                display: "flex",
                alignItems: "baseline",
                gap: "var(--soa-space-3)",
                borderBottom: "1.5px solid var(--soa-border-strong)",
                paddingBottom: "var(--soa-space-2)",
              }}
            >
              <h2
                style={{
                  margin: 0,
                  font: "var(--soa-font-heading-md)",
                  textTransform: "uppercase",
                  letterSpacing: "0.02em",
                }}
              >
                {processName}
              </h2>
              <span
                style={{
                  font: "500 11px/16px var(--soa-font-mono)",
                  color: "var(--soa-text-muted)",
                }}
              >
                {skills.length} SKILL{skills.length === 1 ? "" : "S"} ·{" "}
                {skills.reduce((acc, s) => acc + s.received_30d, 0)} DOCS / 30D
              </span>
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(19rem, 1fr))",
                gap: "var(--soa-space-3)",
              }}
            >
              {skills.map((skill) => (
                <Link
                  key={skill.id}
                  to="/app/$organizationSlug/streams/$streamSlug"
                  params={{ organizationSlug: slug, streamSlug: skill.slug }}
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    border: "1.5px solid var(--soa-border-strong)",
                    background: "var(--soa-surface)",
                    textDecoration: "none",
                    color: "var(--soa-text-primary)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "var(--soa-space-2)",
                      padding: "var(--soa-space-3) var(--soa-space-3) 0",
                    }}
                  >
                    <span
                      style={{
                        font: "500 10px/14px var(--soa-font-mono)",
                        color: "var(--soa-text-muted)",
                      }}
                    >
                      {skill.slug.toUpperCase()}
                    </span>
                    <span style={{ marginLeft: "auto" }}>
                      {skill.trained_version ? (
                        <Badge tone="accent">Trained · v{skill.trained_version}</Badge>
                      ) : (
                        <Badge tone="warning">Needs training</Badge>
                      )}
                    </span>
                  </div>
                  <div
                    style={{
                      padding: "var(--soa-space-1) var(--soa-space-3) var(--soa-space-3)",
                      font: "var(--soa-font-heading-md)",
                      textTransform: "uppercase",
                      letterSpacing: "0.01em",
                    }}
                  >
                    {skill.name}
                  </div>
                  <div
                    style={{
                      marginTop: "auto",
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr 1fr",
                      borderTop: "1px solid var(--soa-border)",
                    }}
                  >
                    {[
                      { k: "ACC", v: accuracyLabel(skill), warn: false },
                      { k: "QUEUE", v: String(skill.in_review), warn: skill.in_review > 20 },
                      { k: "30D", v: String(skill.received_30d), warn: false },
                    ].map((cell, index) => (
                      <div
                        key={cell.k}
                        style={{
                          padding: "var(--soa-space-2) var(--soa-space-3)",
                          borderRight: index < 2 ? "1px solid var(--soa-border)" : "none",
                        }}
                      >
                        <div
                          style={{
                            font: "700 9px/12px var(--soa-font-family)",
                            letterSpacing: "0.1em",
                            color: "var(--soa-text-muted)",
                          }}
                        >
                          {cell.k}
                        </div>
                        <div
                          style={{
                            font: "var(--soa-font-heading-md)",
                            fontVariantNumeric: "tabular-nums",
                            color: cell.warn ? "var(--soa-critical)" : "var(--soa-text-primary)",
                          }}
                        >
                          {cell.v}
                        </div>
                      </div>
                    ))}
                  </div>
                </Link>
              ))}
            </div>
          </section>
        ))}

        {overview.status === "success" && items.length === 0 ? (
          <p style={{ color: "var(--soa-text-muted)" }}>
            No skills yet — create a process and stream to start routing documents.
          </p>
        ) : null}
      </div>
    </AppShell>
  );
}
