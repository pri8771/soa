/**
 * Operational status composites (DSN-007, UI_UX_BLUEPRINT §5.3).
 *
 * Every indicator carries its meaning in TEXT (with an optional icon) —
 * color is reinforcement, never the only signal — and stays legible inside
 * 36–40px dense table rows. Time-dependent indicators take ``now`` as a
 * parameter so rendering is deterministic and testable.
 */

import type { ReactNode } from "react";

import { Badge, ProgressBar, type StatusTone } from "./status";

// ---------------------------------------------------------------------------
// Document status
// ---------------------------------------------------------------------------

export type DocumentState =
  | "received"
  | "processing"
  | "review_required"
  | "approved"
  | "rejected"
  | "failed"
  | "quarantined"
  | "exported"
  | "cancelled";

const DOCUMENT_STATE_DISPLAY: Record<DocumentState, { label: string; tone: StatusTone }> = {
  received: { label: "Received", tone: "neutral" },
  processing: { label: "Processing", tone: "info" },
  review_required: { label: "Needs review", tone: "warning" },
  approved: { label: "Approved", tone: "success" },
  rejected: { label: "Rejected", tone: "critical" },
  failed: { label: "Failed", tone: "critical" },
  quarantined: { label: "Quarantined", tone: "warning" },
  exported: { label: "Exported", tone: "success" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export function StatusIndicator({ state }: { state: DocumentState }) {
  const display = DOCUMENT_STATE_DISPLAY[state];
  return <Badge tone={display.tone}>{display.label}</Badge>;
}

// ---------------------------------------------------------------------------
// Confidence
// ---------------------------------------------------------------------------

export type ConfidenceLevel = "high" | "medium" | "low";

export function confidenceLevel(value: number): ConfidenceLevel {
  if (value >= 0.9) return "high";
  if (value >= 0.7) return "medium";
  return "low";
}

const CONFIDENCE_DISPLAY: Record<ConfidenceLevel, { label: string; tone: StatusTone }> = {
  high: { label: "High", tone: "success" },
  medium: { label: "Medium", tone: "warning" },
  low: { label: "Low", tone: "critical" },
};

/** Confidence with numeric detail — never color-only (§4.2). */
export function ConfidenceIndicator({
  value,
  explanation,
}: {
  /** Calibrated platform confidence in [0, 1]. */
  value: number;
  explanation?: string;
}) {
  const level = confidenceLevel(value);
  const display = CONFIDENCE_DISPLAY[level];
  const percent = Math.round(value * 100);
  return (
    <Badge tone={display.tone} className="soa-confidence">
      <span title={explanation}>
        {display.label} · {percent}%
      </span>
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// SLA
// ---------------------------------------------------------------------------

export function formatSlaRemaining(dueAt: Date, now: Date): string {
  const deltaMs = dueAt.getTime() - now.getTime();
  const absMinutes = Math.round(Math.abs(deltaMs) / 60_000);
  const hours = Math.floor(absMinutes / 60);
  const minutes = absMinutes % 60;
  const span = hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
  return deltaMs >= 0 ? `${span} left` : `breached ${span} ago`;
}

export function SlaIndicator({
  dueAt,
  now,
  atRiskMinutes = 60,
}: {
  dueAt: Date;
  now: Date;
  atRiskMinutes?: number;
}) {
  const deltaMinutes = (dueAt.getTime() - now.getTime()) / 60_000;
  const tone: StatusTone =
    deltaMinutes < 0 ? "critical" : deltaMinutes <= atRiskMinutes ? "warning" : "neutral";
  return <Badge tone={tone}>SLA {formatSlaRemaining(dueAt, now)}</Badge>;
}

// ---------------------------------------------------------------------------
// Validation summary
// ---------------------------------------------------------------------------

export function ValidationSummary({ errors, warnings }: { errors: number; warnings: number }) {
  if (errors === 0 && warnings === 0) {
    return <Badge tone="success">Valid</Badge>;
  }
  return (
    <span className="soa-validation-summary">
      {errors > 0 ? (
        <Badge tone="critical">
          {errors} {errors === 1 ? "error" : "errors"}
        </Badge>
      ) : null}
      {warnings > 0 ? (
        <Badge tone="warning">
          {warnings} {warnings === 1 ? "warning" : "warnings"}
        </Badge>
      ) : null}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Stage progress
// ---------------------------------------------------------------------------

export interface Stage {
  id: string;
  label: string;
  status: "done" | "active" | "pending" | "failed";
}

/** Compact ordered stage list (processing pipeline visibility). */
export function StageProgress({ stages, label }: { stages: Stage[]; label: string }) {
  return (
    <ol className="soa-stage-progress" aria-label={label}>
      {stages.map((stage) => (
        <li
          key={stage.id}
          className="soa-stage"
          data-status={stage.status}
          aria-current={stage.status === "active" ? "step" : undefined}
        >
          <span aria-hidden="true" className="soa-stage-marker">
            {stage.status === "done" ? "✓" : stage.status === "failed" ? "✕" : "•"}
          </span>
          <span className="soa-stage-label">
            {stage.label}
            {stage.status === "failed" ? " (failed)" : ""}
          </span>
        </li>
      ))}
    </ol>
  );
}

// ---------------------------------------------------------------------------
// Connection status
// ---------------------------------------------------------------------------

export type ConnectionHealth = "healthy" | "degraded" | "failed";

const CONNECTION_DISPLAY: Record<ConnectionHealth, { label: string; tone: StatusTone }> = {
  healthy: { label: "Healthy", tone: "success" },
  degraded: { label: "Degraded", tone: "warning" },
  failed: { label: "Failing", tone: "critical" },
};

export function ConnectionStatusCard({
  name,
  health,
  lastCheckedLabel,
  children,
}: {
  name: string;
  health: ConnectionHealth;
  lastCheckedLabel: string;
  children?: ReactNode;
}) {
  const display = CONNECTION_DISPLAY[health];
  return (
    <div className="soa-connection-card" data-health={health}>
      <div className="soa-connection-head">
        <span className="soa-connection-name">{name}</span>
        <Badge tone={display.tone}>{display.label}</Badge>
      </div>
      <p className="soa-connection-checked">Last checked {lastCheckedLabel}</p>
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Usage meter
// ---------------------------------------------------------------------------

export function UsageMeter({
  label,
  used,
  limit,
  unit = "",
  warnAtFraction = 0.8,
}: {
  label: string;
  used: number;
  limit: number;
  unit?: string;
  warnAtFraction?: number;
}) {
  const fraction = limit > 0 ? used / limit : 0;
  const nearLimit = fraction >= warnAtFraction;
  return (
    <div className="soa-usage-meter" data-near-limit={nearLimit}>
      <div className="soa-usage-meter-head">
        <span className="soa-usage-meter-label">{label}</span>
        <span className="soa-usage-meter-value">
          {used.toLocaleString("en-GB")} / {limit.toLocaleString("en-GB")} {unit}
          {nearLimit ? " — near limit" : ""}
        </span>
      </div>
      <ProgressBar label={`${label} usage`} value={Math.min(fraction * 100, 100)} />
    </div>
  );
}
