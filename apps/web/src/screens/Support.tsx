/**
 * Support intake and severity workflow (GTM-006).
 *
 * The customer-facing entry point for getting help: a stable support
 * reference to quote, the shared severity model (aligned with the
 * REL-011 incident-communication severities so a customer's "this is
 * urgent" and our on-call's page speak the same language), guidance on
 * the SECURE attachment path (documents are confidential — never email
 * them), and a composer that assembles a copy-ready request.
 *
 * Honesty: this is the intake side. The staff-side triage console — where
 * support engineers with time-limited, audited access work a ticket — is
 * ANA-008, which is gated on the production identity decision (OPEN-002);
 * until it lands, requests are composed here and sent through the support
 * channel named in your plan. Response/resolution TARGETS are set by your
 * support plan and are not invented here.
 */

import { Badge, Banner, Select, TextField } from "@soa/design-system";
import { Link } from "@tanstack/react-router";
import { useMemo, useState, type CSSProperties } from "react";

import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

interface Severity {
  key: string;
  label: string;
  definition: string;
  owner: string;
}

// Aligned with docs/INCIDENT_COMMUNICATION.md (REL-011).
const SEVERITIES: Severity[] = [
  {
    key: "sev1",
    label: "SEV1 — Critical",
    definition: "Data at risk (loss or exposure), or the platform is unusable for your whole team.",
    owner: "Paged to on-call immediately; an incident lead and comms owner are named.",
  },
  {
    key: "sev2",
    label: "SEV2 — Significant",
    definition:
      "A core flow — intake, review, or export — is impaired for many users, or you are hard-down.",
    owner: "Paged to on-call; status updates at each state change.",
  },
  {
    key: "sev3",
    label: "SEV3 — Minor",
    definition: "A single-user or contained issue, degraded-not-down, or a workaround exists.",
    owner: "Ticketed and worked in business hours.",
  },
];

const card: CSSProperties = {
  border: "1px solid var(--soa-border)",
  borderRadius: "var(--soa-radius-panel)",
  padding: "var(--soa-space-4)",
  display: "grid",
  gap: "var(--soa-space-3)",
};

export function Support() {
  const session = useShellSession();
  const org = session.organization;
  const [severity, setSeverity] = useState<string>("sev3");
  const [summary, setSummary] = useState<string>("");

  // A stable, non-secret reference the customer quotes so support can
  // locate the tenant without the customer pasting anything sensitive.
  const supportReference = `SOA-${org.slug.toUpperCase()}`;

  const composed = useMemo(() => {
    const sev = SEVERITIES.find((s) => s.key === severity)?.label ?? severity;
    return [
      `Support reference: ${supportReference}`,
      `Organization: ${org.name}`,
      `Severity: ${sev}`,
      `Summary: ${summary || "(describe what happened, what you expected, and steps to reproduce)"}`,
      "",
      "Do NOT paste document contents or credentials here. If a specific",
      "document is involved, quote its reference from the Documents screen;",
      "support can open it under audited access.",
    ].join("\n");
  }, [severity, summary, supportReference, org.name]);

  return (
    <AppShell title="Support" breadcrumbs={[{ label: org.name }, { label: "Support" }]}>
      <div style={{ display: "grid", gap: "var(--soa-space-4)", maxWidth: "56rem" }}>
        <section style={card}>
          <strong>Your support reference</strong>
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap" }}>
            <code
              style={{
                font: "var(--soa-font-mono, monospace)",
                fontSize: "1.1rem",
                padding: "0.25rem 0.5rem",
                border: "1px solid var(--soa-border)",
                borderRadius: "var(--soa-radius-control)",
              }}
            >
              {supportReference}
            </code>
            <span style={{ color: "var(--soa-text-muted)", fontSize: "0.9rem" }}>
              Quote this when you contact support so we can find your organization instantly.
            </span>
          </div>
        </section>

        <section style={card}>
          <strong>How urgent is it?</strong>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.75rem" }}>
            {SEVERITIES.map((sev) => (
              <li
                key={sev.key}
                style={{
                  display: "grid",
                  gap: "0.25rem",
                  borderLeft: "3px solid var(--soa-border)",
                  paddingLeft: "0.75rem",
                }}
              >
                <span style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                  <Badge
                    tone={
                      sev.key === "sev1" ? "critical" : sev.key === "sev2" ? "warning" : "neutral"
                    }
                  >
                    {sev.label}
                  </Badge>
                </span>
                <span style={{ fontSize: "0.9rem" }}>{sev.definition}</span>
                <span style={{ color: "var(--soa-text-muted)", fontSize: "0.85rem" }}>
                  {sev.owner}
                </span>
              </li>
            ))}
          </ul>
          <span style={{ color: "var(--soa-text-muted)", fontSize: "0.85rem" }}>
            Response and resolution targets are set by your support plan.
          </span>
        </section>

        <Banner tone="info" title="Keep confidential data out of support messages">
          Purchase orders are confidential. Never email or paste document contents or credentials.
          Reference a document by its id from the{" "}
          <Link to="/app/$organizationSlug/documents" params={{ organizationSlug: org.slug }}>
            Documents
          </Link>{" "}
          screen — support can open it under time-limited, audited access.
        </Banner>

        <section style={card}>
          <strong>Compose a request</strong>
          <div style={{ maxWidth: "22rem" }}>
            <Select
              label="Severity"
              items={SEVERITIES.map((s) => ({ id: s.key, label: s.label }))}
              selectedKey={severity}
              onSelectionChange={(key) => setSeverity(String(key))}
            />
          </div>
          <TextField
            label="Summary"
            value={summary}
            onChange={setSummary}
            placeholder="What happened, what you expected, and how to reproduce it"
          />
          <label style={{ display: "grid", gap: "0.25rem" }}>
            <span style={{ fontSize: "0.85rem", color: "var(--soa-text-muted)" }}>
              Copy this into your support channel
            </span>
            <textarea
              readOnly
              value={composed}
              rows={9}
              style={{
                width: "100%",
                font: "var(--soa-font-mono, monospace)",
                fontSize: "0.85rem",
                padding: "0.5rem",
                border: "1px solid var(--soa-border)",
                borderRadius: "var(--soa-radius-control)",
                background: "var(--soa-surface-muted, transparent)",
                resize: "vertical",
              }}
            />
          </label>
        </section>
      </div>
    </AppShell>
  );
}
