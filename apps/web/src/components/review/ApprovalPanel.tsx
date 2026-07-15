/**
 * Approval panel (REV-013): the completion summary and the
 * approve / reject / escalate actions for a review task.
 *
 * Safety properties this component enforces:
 * - Approving is ALWAYS two explicit steps (open the summary, then
 *   confirm) — no keyboard shortcut or stray Enter can approve.
 * - Every disabled action says WHY it is disabled, next to the control.
 * - Critical blockers surface an override-reason field only for users
 *   who hold the override permission; everyone else is told what is
 *   missing instead of hitting a server error.
 * - Outcomes are announced through an aria-live region for screen
 *   readers, in addition to the visible banner.
 */

import { Badge, Banner, Button } from "@soa/design-system";
import { useId, useState } from "react";

export interface ApprovalDecisionSummary {
  route: string;
  reasons: Record<string, unknown>[];
}

export interface ApprovalPanelProps {
  /** The current task can be edited by this user (in_progress + assignee). */
  editable: boolean;
  /** Shown as the disabled reason when not editable. */
  readOnlyReason: string | null;
  /** Whether unresolved CRITICAL blockers remain (task/blocking or the
   * latest revalidation's evaluation.blocking). */
  blocking: boolean;
  /** The latest route decision; its reasons are the remaining warnings. */
  decision: ApprovalDecisionSummary | null;
  canApprove: boolean;
  canOverride: boolean;
  canReject: boolean;
  /** Where an approved order goes (the document's stream). */
  destination: string | null;
  /** Set when the task is already settled: "approved" / "rejected" / etc. */
  settledOutcome: string | null;
  busy: boolean;
  /** A correction is queued/saving or at least one correction failed. */
  approvalBlockedReason: string | null;
  /** The latest action outcome; rendered visibly AND announced politely. */
  statusMessage: string | null;
  onApprove: (overrideReason: string | null) => void;
  onReject: (reason: string) => void;
  onEscalate: (reason: string) => void;
}

type PanelMode = "closed" | "approve" | "reject" | "escalate";

const label = { font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" } as const;

function DisabledReason({ id, children }: { id: string; children: string }) {
  return (
    <p id={id} style={{ ...label, margin: 0 }}>
      {children}
    </p>
  );
}

export function ApprovalPanel(props: ApprovalPanelProps) {
  const [mode, setMode] = useState<PanelMode>("closed");
  const [overrideReason, setOverrideReason] = useState("");
  const [rejectReason, setRejectReason] = useState("");
  const [escalateReason, setEscalateReason] = useState("");
  const headingId = useId();
  const approveReasonId = useId();
  const rejectReasonId = useId();
  const escalateReasonId = useId();

  const warnings = props.decision?.reasons ?? [];

  const approveDisabledReason = !props.canApprove
    ? "You do not have the documents.approve permission."
    : !props.editable
      ? (props.readOnlyReason ?? "The task is not claimed by you.")
      : props.approvalBlockedReason;
  const rejectDisabledReason = !props.canReject
    ? "Rejecting needs the documents.reject permission (supervisors)."
    : !props.editable
      ? (props.readOnlyReason ?? "The task is not claimed by you.")
      : null;

  const confirmBlockedReason =
    props.blocking && !props.canOverride
      ? "Critical blockers remain, and overriding them requires the " +
        "documents.approve.override permission. Resolve the blockers or escalate."
      : props.blocking && overrideReason.trim() === ""
        ? "Critical blockers remain. Enter the override reason to enable approval."
        : null;

  if (props.settledOutcome !== null) {
    return (
      <section aria-labelledby={headingId}>
        <h2 id={headingId} style={{ font: "var(--soa-font-heading-sm)", margin: 0 }}>
          Complete review
        </h2>
        <div role="status" aria-live="polite">
          <Banner
            tone={props.settledOutcome === "approved" ? "success" : "info"}
            title={`This task is ${props.settledOutcome}`}
          >
            {props.settledOutcome === "approved"
              ? "The order was approved and handed to export."
              : props.settledOutcome === "rejected"
                ? "The document was rejected and will not export."
                : "No further review actions are available."}
          </Banner>
        </div>
      </section>
    );
  }

  return (
    <section
      aria-labelledby={headingId}
      style={{ display: "grid", gap: "var(--soa-space-3)" }}
      data-testid="approval-panel"
    >
      <h2 id={headingId} style={{ font: "var(--soa-font-heading-sm)", margin: 0 }}>
        Complete review
      </h2>

      {/* Screen-reader announcement + visible outcome of the last action. */}
      <div role="status" aria-live="polite">
        {props.statusMessage ? <p style={{ margin: 0 }}>{props.statusMessage}</p> : null}
      </div>

      <div style={{ display: "flex", gap: "var(--soa-space-2)", flexWrap: "wrap" }}>
        <Button
          variant="primary"
          size="sm"
          data-review-action="approve"
          isDisabled={approveDisabledReason !== null || props.busy}
          aria-expanded={mode === "approve"}
          onPress={() => setMode(mode === "approve" ? "closed" : "approve")}
        >
          Approve order…
        </Button>
        <Button
          variant="destructive"
          size="sm"
          data-review-action="reject"
          isDisabled={rejectDisabledReason !== null || props.busy}
          aria-expanded={mode === "reject"}
          onPress={() => setMode(mode === "reject" ? "closed" : "reject")}
        >
          Reject…
        </Button>
        <Button
          size="sm"
          data-review-action="escalate"
          isDisabled={props.busy}
          aria-expanded={mode === "escalate"}
          onPress={() => setMode(mode === "escalate" ? "closed" : "escalate")}
        >
          Escalate…
        </Button>
      </div>
      {[...new Set([approveDisabledReason, rejectDisabledReason].filter(Boolean))].map(
        (reason, index) => (
          <DisabledReason key={index} id={`${approveReasonId}-why-${index}`}>
            {String(reason)}
          </DisabledReason>
        ),
      )}

      {mode === "approve" && approveDisabledReason === null ? (
        <div
          style={{
            display: "grid",
            gap: "var(--soa-space-2)",
            border: "1px solid var(--soa-border)",
            borderRadius: "var(--soa-radius-md)",
            padding: "var(--soa-space-3)",
          }}
        >
          <p style={{ margin: 0, font: "var(--soa-font-body-sm)" }}>
            <strong>Completion summary.</strong>{" "}
            {warnings.length === 0
              ? "All validations pass; nothing remains open."
              : `${warnings.length} finding(s) remain and will be recorded on the approval:`}
          </p>
          {warnings.length > 0 ? (
            <ul style={{ margin: 0, paddingInlineStart: "1.2rem" }}>
              {warnings.map((warning, index) => (
                <li key={index} style={{ font: "var(--soa-font-body-sm)" }}>
                  <Badge tone={props.blocking ? "critical" : "warning"}>
                    {String(warning["rule_key"] ?? warning["field_key"] ?? warning["code"])}
                  </Badge>{" "}
                  {String(warning["message"] ?? "")}
                </li>
              ))}
            </ul>
          ) : null}
          <p style={{ ...label, margin: 0 }}>
            {props.destination
              ? `On approval, this order is handed to export for stream “${props.destination}”.`
              : "On approval, this order is handed to export."}
          </p>
          {props.blocking && props.canOverride ? (
            <div style={{ display: "grid", gap: "var(--soa-space-1)" }}>
              <label htmlFor={`${approveReasonId}-input`} style={label}>
                Override reason (required — critical blockers remain)
              </label>
              <textarea
                id={`${approveReasonId}-input`}
                value={overrideReason}
                onChange={(event) => setOverrideReason(event.target.value)}
                rows={2}
                style={{ font: "var(--soa-font-body-sm)" }}
              />
            </div>
          ) : null}
          {confirmBlockedReason !== null ? (
            <DisabledReason id={`${approveReasonId}-confirm`}>
              {confirmBlockedReason}
            </DisabledReason>
          ) : null}
          <div>
            <Button
              variant="primary"
              size="sm"
              isDisabled={confirmBlockedReason !== null || props.busy}
              onPress={() => {
                const reason = overrideReason.trim();
                props.onApprove(props.blocking && reason !== "" ? reason : null);
                setMode("closed");
              }}
            >
              Confirm approval
            </Button>
          </div>
        </div>
      ) : null}

      {mode === "reject" && rejectDisabledReason === null ? (
        <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
          <label htmlFor={`${rejectReasonId}-input`} style={label}>
            Rejection reason (required; it becomes the document’s status reason)
          </label>
          <textarea
            id={`${rejectReasonId}-input`}
            value={rejectReason}
            onChange={(event) => setRejectReason(event.target.value)}
            rows={2}
            style={{ font: "var(--soa-font-body-sm)" }}
          />
          <div>
            <Button
              variant="destructive"
              size="sm"
              isDisabled={rejectReason.trim() === "" || props.busy}
              onPress={() => {
                props.onReject(rejectReason.trim());
                setMode("closed");
              }}
            >
              Confirm rejection
            </Button>
          </div>
        </div>
      ) : null}
      {mode === "escalate" ? (
        <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
          <label htmlFor={`${escalateReasonId}-input`} style={label}>
            Escalation reason (required; the task returns to the queue at top priority)
          </label>
          <textarea
            id={`${escalateReasonId}-input`}
            value={escalateReason}
            onChange={(event) => setEscalateReason(event.target.value)}
            rows={2}
            style={{ font: "var(--soa-font-body-sm)" }}
          />
          <div>
            <Button
              size="sm"
              isDisabled={escalateReason.trim() === "" || props.busy}
              onPress={() => {
                props.onEscalate(escalateReason.trim());
                setMode("closed");
              }}
            >
              Confirm escalation
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
