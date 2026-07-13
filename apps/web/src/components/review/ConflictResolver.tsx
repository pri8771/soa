/**
 * Review conflict resolver (REV-014).
 *
 * When a save is refused because the task changed under the reviewer
 * (409 on the optimistic version), NOTHING is lost: every refused edit
 * is held here, side by side with the value the server has now and who
 * put it there, until the reviewer explicitly chooses per field:
 *
 * - "Keep mine" re-saves the held value against the FRESH task version
 *   (a merge — the other editor's other fields stay intact), or
 * - "Use server value" drops the held edit in favor of the server's.
 *
 * A discard-all reload stays available, clearly labeled as discarding.
 * The current task state is always visible so the reviewer knows they
 * still own the task (or that it was claimed/completed elsewhere).
 */

import { Banner, Button } from "@soa/design-system";

export interface ConflictEntry {
  fieldKey: string;
  rowIndex: number | null;
  /** The value the reviewer tried to save (held, not lost). */
  yourValue: string;
  /** What the server has now (latest correction, else the extraction). */
  serverValue: string | null;
  /** Who authored the server's value, when a correction did (metadata). */
  serverEditor: string | null;
}

export interface ConflictResolverProps {
  message: string;
  conflicts: ConflictEntry[];
  /** Current task state after the refresh, e.g. "in progress — assigned
   * to you (version 9)". Keeps the task's status unambiguous. */
  taskStatus: string;
  onKeepMine: (entry: ConflictEntry) => void;
  onTakeServer: (entry: ConflictEntry) => void;
  onReloadDiscardingAll: () => void;
}

function show(value: string | null): string {
  return value === null || value === "" ? "(empty)" : `“${value}”`;
}

export function ConflictResolver(props: ConflictResolverProps) {
  return (
    <Banner tone="critical" title="Someone else changed this task">
      <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
        <p style={{ margin: 0 }}>{props.message}</p>
        <p style={{ margin: 0, font: "var(--soa-font-caption)" }}>Task: {props.taskStatus}</p>
        {props.conflicts.length > 0 ? (
          <ul
            style={{
              margin: 0,
              paddingInlineStart: 0,
              listStyle: "none",
              display: "grid",
              gap: "var(--soa-space-2)",
            }}
            aria-label="Unsaved edits awaiting a decision"
          >
            {props.conflicts.map((entry) => (
              <li
                key={`${entry.fieldKey}#${entry.rowIndex ?? "h"}`}
                style={{ display: "grid", gap: "var(--soa-space-1)" }}
              >
                <span style={{ font: "var(--soa-font-body-sm)" }}>
                  <strong>
                    {entry.fieldKey}
                    {entry.rowIndex !== null ? ` (row ${entry.rowIndex + 1})` : ""}
                  </strong>{" "}
                  — yours: {show(entry.yourValue)} · server: {show(entry.serverValue)}
                  {entry.serverEditor ? ` (corrected by ${entry.serverEditor})` : ""}
                </span>
                <span style={{ display: "flex", gap: "var(--soa-space-2)" }}>
                  <Button size="sm" variant="primary" onPress={() => props.onKeepMine(entry)}>
                    {`Keep mine: ${entry.fieldKey}`}
                  </Button>
                  <Button size="sm" onPress={() => props.onTakeServer(entry)}>
                    {`Use server value: ${entry.fieldKey}`}
                  </Button>
                </span>
              </li>
            ))}
          </ul>
        ) : null}
        <div>
          <Button size="sm" variant="destructive" onPress={props.onReloadDiscardingAll}>
            Reload the workspace (discards the edits above)
          </Button>
        </div>
      </div>
    </Banner>
  );
}
