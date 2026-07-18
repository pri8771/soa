/**
 * Header field editor (REV-007, UI_UX_BLUEPRINT §5.7).
 *
 * One row per header field: raw and canonical values, provider
 * confidence, validation status, provenance (provider@model), rival
 * candidate readings (one click adopts one), and the field's evidence —
 * focusing a field highlights its source in the viewer, and the reason
 * navigator walks the task's reasons straight to the fields they name.
 *
 * Edits autosave on blur or Enter through the REV-009 corrections
 * endpoint; each field shows its save state (saving / saved / failed
 * with the server's reason) and the server's normalization verdict IS
 * the validation — an uninterpretable value comes back flagged, never
 * silently accepted. A stale-version conflict (someone else edited the
 * task) surfaces as a banner with a reload action; nothing is lost
 * because nothing was saved.
 *
 * Keyboard map (documented on the editor itself): Enter saves the
 * focused field, Escape reverts the draft, Alt+ArrowDown / Alt+ArrowUp
 * move between fields, Alt+R jumps to the next unresolved reason.
 */

import { Badge, Banner, Button } from "@soa/design-system";
import { useRef, useState } from "react";

import type { ReviewReason, WorkspaceField } from "../../api/client";

const KEYBOARD_MAP =
  "Keyboard: Enter saves the field, Escape reverts, Alt+ArrowDown next field, " +
  "Alt+ArrowUp previous field, Alt+R next reason";

export type SaveState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "saved" }
  | { status: "error"; message: string };

const STATUS_TONES: Record<string, "neutral" | "success" | "warning" | "critical" | "info"> = {
  passed: "success",
  review: "warning",
  blocked: "critical",
  pending: "neutral",
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    const money = value as { amount?: string; currency?: string | null };
    if (money.amount !== undefined) {
      return `${money.amount}${money.currency ? ` ${money.currency}` : ""}`;
    }
    return JSON.stringify(value);
  }
  return String(value);
}

export function HeaderFieldEditor({
  fields,
  reasons,
  drafts,
  saveStates,
  activeFieldKey,
  onFieldFocus,
  onDraftChange,
  onSave,
  readOnly,
}: {
  fields: WorkspaceField[];
  reasons: ReviewReason[];
  /** Current draft per field key (undefined = untouched). */
  drafts: Record<string, string>;
  saveStates: Record<string, SaveState>;
  activeFieldKey: string | null;
  onFieldFocus: (fieldKey: string) => void;
  onDraftChange: (fieldKey: string, value: string) => void;
  onSave: (fieldKey: string, value: string) => void;
  readOnly?: boolean;
}) {
  const [reasonIndex, setReasonIndex] = useState(0);
  const inputs = useRef(new Map<string, HTMLInputElement>());
  const fieldReasons = reasons.filter((reason) => reason.field_key !== null);

  const focusField = (fieldKey: string) => {
    onFieldFocus(fieldKey);
    inputs.current.get(fieldKey)?.focus();
  };

  const goToReason = (index: number) => {
    if (fieldReasons.length === 0) return;
    const bounded = ((index % fieldReasons.length) + fieldReasons.length) % fieldReasons.length;
    setReasonIndex(bounded);
    const key = fieldReasons[bounded].field_key;
    if (key) focusField(key);
  };

  const moveFocus = (fromKey: string, delta: number) => {
    const index = fields.findIndex((field) => field.field_key === fromKey);
    const target = fields[index + delta];
    if (target) focusField(target.field_key);
  };

  return (
    <section
      aria-label="Header fields"
      aria-description={KEYBOARD_MAP}
      style={{ display: "grid", gap: "var(--soa-space-3)" }}
    >
      {fieldReasons.length > 0 ? (
        <div
          style={{
            display: "flex",
            gap: "var(--soa-space-2)",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <Button size="sm" variant="secondary" onPress={() => goToReason(reasonIndex)}>
            Go to reason {reasonIndex + 1} of {fieldReasons.length}
          </Button>
          <Button
            size="sm"
            variant="subtle"
            aria-label="Next reason"
            onPress={() => goToReason(reasonIndex + 1)}
          >
            Next reason
          </Button>
          <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
            {fieldReasons[reasonIndex]?.message}
          </span>
        </div>
      ) : null}

      <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "1px" }}>
        {fields.map((field) => {
          const draft = drafts[field.field_key];
          const currentValue = draft ?? field.raw_value ?? "";
          const save = saveStates[field.field_key] ?? { status: "idle" };
          const flagged = fieldReasons.some((reason) => reason.field_key === field.field_key);
          const active = activeFieldKey === field.field_key;
          const inputId = `field-${field.field_key}`;
          return (
            <li
              key={field.field_key}
              style={{
                borderLeft: active ? "3px solid var(--soa-accent)" : "3px solid var(--soa-border)",
                borderBottom: "1px solid var(--soa-border)",
                background: active ? "var(--soa-accent-soft)" : "var(--soa-surface)",
                padding: "var(--soa-space-3) var(--soa-space-4)",
                display: "grid",
                gap: "var(--soa-space-2)",
              }}
            >
              <div
                style={{
                  display: "flex",
                  gap: "var(--soa-space-2)",
                  alignItems: "center",
                  flexWrap: "wrap",
                  justifyContent: "space-between",
                }}
              >
                <label
                  htmlFor={inputId}
                  style={{
                    font: "700 11px/16px var(--soa-font-family)",
                    letterSpacing: "0.08em",
                    textTransform: "uppercase",
                    color: "var(--soa-text-secondary)",
                  }}
                >
                  {field.field_key.replace(/_/g, " ")}
                </label>
                <div
                  style={{
                    display: "flex",
                    gap: "var(--soa-space-2)",
                    alignItems: "center",
                    flexWrap: "wrap",
                  }}
                >
                  {flagged ? <Badge tone="warning">needs attention</Badge> : null}
                  <Badge tone={STATUS_TONES[field.validation_status] ?? "neutral"}>
                    {field.validation_status}
                  </Badge>
                  {/* Autosave state, announced to screen readers. */}
                  <span
                    aria-live="polite"
                    style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}
                  >
                    {save.status === "saving"
                      ? "Saving…"
                      : save.status === "saved"
                        ? "Saved"
                        : save.status === "error"
                          ? `Not saved: ${save.message}`
                          : ""}
                  </span>
                </div>
              </div>

              <input
                id={inputId}
                className="soa-field-input"
                data-review-field-key={field.field_key}
                data-review-row-index="header"
                ref={(element) => {
                  if (element) inputs.current.set(field.field_key, element);
                  else inputs.current.delete(field.field_key);
                }}
                value={currentValue}
                readOnly={readOnly}
                onFocus={() => onFieldFocus(field.field_key)}
                onChange={(event) => onDraftChange(field.field_key, event.target.value)}
                onBlur={() => {
                  if (draft !== undefined && draft !== (field.raw_value ?? "")) {
                    onSave(field.field_key, draft);
                  }
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    if (draft !== undefined) onSave(field.field_key, draft);
                  } else if (event.key === "Escape") {
                    onDraftChange(field.field_key, field.raw_value ?? "");
                  } else if (event.altKey && event.key === "ArrowDown") {
                    event.preventDefault();
                    moveFocus(field.field_key, 1);
                  } else if (event.altKey && event.key === "ArrowUp") {
                    event.preventDefault();
                    moveFocus(field.field_key, -1);
                  } else if (event.altKey && event.key.toLowerCase() === "r") {
                    event.preventDefault();
                    goToReason(reasonIndex + 1);
                  }
                }}
                style={{ width: "100%" }}
              />

              <div
                style={{
                  display: "flex",
                  gap: "var(--soa-space-3)",
                  flexWrap: "wrap",
                  font: "500 11px/16px var(--soa-font-mono, monospace)",
                  color: "var(--soa-text-muted)",
                }}
              >
                <span>
                  confidence {Math.round(field.confidence * 100)}% · {field.provider}
                  {field.provider_model ? `@${field.provider_model}` : ""}
                </span>
                <span>raw: {field.raw_value ?? "—"}</span>
                <span>canonical: {formatValue(field.normalized_value)}</span>
                {field.normalization_error ? (
                  <span role="alert" style={{ color: "var(--soa-critical)" }}>
                    {field.normalization_error}
                  </span>
                ) : null}
                {field.evidence.length === 0 ? <span>no source evidence</span> : null}
              </div>

              {field.candidates.length > 0 ? (
                <div
                  style={{
                    display: "flex",
                    gap: "var(--soa-space-2)",
                    alignItems: "center",
                    flexWrap: "wrap",
                  }}
                >
                  <span style={{ font: "var(--soa-font-caption)" }}>Also read as:</span>
                  {field.candidates.map((candidate) => (
                    <Button
                      key={candidate.raw_value}
                      size="sm"
                      variant="subtle"
                      isDisabled={readOnly}
                      onPress={() => onSave(field.field_key, candidate.raw_value)}
                    >
                      {candidate.raw_value} ({Math.round(candidate.confidence * 100)}%)
                    </Button>
                  ))}
                </div>
              ) : null}
            </li>
          );
        })}
      </ol>
      {fields.length === 0 ? (
        <Banner tone="info" title="No header fields">
          This run extracted no header fields.
        </Banner>
      ) : null}
    </section>
  );
}
