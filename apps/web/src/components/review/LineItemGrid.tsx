/**
 * Line-item grid (REV-008, UI_UX_BLUEPRINT §5.8).
 *
 * A windowed, editable grid over the run's line items: only the rows in
 * view render (hundreds of rows stay responsive), the first column is
 * frozen, and every cell edit persists through the same append-only
 * corrections channel as the header editor — row identity is the stable
 * extraction row_index, never a display position.
 *
 * Row operations are compositions of cell corrections, so they are all
 * durable and all UNDOABLE: remove clears the row's cells, add/split
 * write cells onto a fresh row index, merge folds quantity and line
 * total into the row above and clears the source. The undo stack
 * replays the previous values as new corrections — history stays
 * append-only, nothing is destroyed.
 *
 * Keyboard map (documented on the grid): Enter saves and moves down,
 * Escape reverts the cell, Tab moves across. Paste accepts
 * tab-separated cells and fills rightward from the focused column.
 */

import { Badge, Button } from "@soa/design-system";
import { useRef, useState } from "react";

import type { SaveState } from "./HeaderFieldEditor";

const ROW_HEIGHT = 44;
const OVERSCAN = 5;
const VIEWPORT_ROWS = 10;

const GRID_KEYBOARD_MAP =
  "Keyboard: Enter saves the cell and moves down, Escape reverts, Tab moves across. " +
  "Paste fills tab-separated cells rightward.";

export interface GridCell {
  value: string | null;
  corrected: boolean;
  normalizationError: string | null;
  hasEvidence: boolean;
}

export interface GridRow {
  rowIndex: number;
  cells: Record<string, GridCell>;
}

function columnLabel(key: string): string {
  return key.split(".").pop()?.replace(/_/g, " ") ?? key;
}

function numeric(value: string | null): number | null {
  if (value === null) return null;
  const parsed = Number(value.replace(/,/g, ""));
  return Number.isFinite(parsed) ? parsed : null;
}

export function LineItemGrid({
  table,
  columns,
  rows,
  saveStates,
  onSaveCell,
  onCellFocus,
  onAddRow,
  onRemoveRow,
  onSplitRow,
  onMergeUp,
  onUndo,
  canUndo,
  readOnly,
}: {
  table: string;
  columns: string[];
  rows: GridRow[];
  saveStates: Record<string, SaveState>;
  onSaveCell: (fieldKey: string, rowIndex: number, value: string) => void;
  onCellFocus: (fieldKey: string, rowIndex: number) => void;
  onAddRow: () => void;
  onRemoveRow: (rowIndex: number) => void;
  onSplitRow: (rowIndex: number) => void;
  onMergeUp: (rowIndex: number) => void;
  onUndo: () => void;
  canUndo: boolean;
  readOnly?: boolean;
}) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [scrollTop, setScrollTop] = useState(0);
  const inputs = useRef(new Map<string, HTMLInputElement>());
  //: Last value saved per cell: Enter saves and moves focus, and the
  //: blur that follows must not save the same draft again.
  const lastSaved = useRef(new Map<string, string>());
  const skipNextBlur = useRef(new Set<string>());

  const start = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(rows.length, start + VIEWPORT_ROWS + OVERSCAN * 2);
  const visible = rows.slice(start, end);

  const cellId = (key: string, rowIndex: number) => `${key}#${rowIndex}`;

  const saveDraft = (key: string, rowIndex: number): boolean => {
    const id = cellId(key, rowIndex);
    const draft = drafts[id];
    const current = rows.find((row) => row.rowIndex === rowIndex)?.cells[key]?.value ?? "";
    // Enter followed by blur must not duplicate a live/successful request,
    // but a failed request MUST be retryable without forcing the reviewer to
    // change the value and change it back first.
    const duplicateUnlessFailed =
      lastSaved.current.get(id) === draft && saveStates[id]?.status !== "error";
    if (draft === undefined || draft === (current ?? "") || duplicateUnlessFailed) {
      return false;
    }
    lastSaved.current.set(id, draft);
    onSaveCell(key, rowIndex, draft);
    return true;
  };

  const totals = columns.map((key) => {
    const values = rows
      .map((row) => numeric(row.cells[key]?.value ?? null))
      .filter((value): value is number => value !== null);
    if (values.length === 0) return null;
    return values.reduce((sum, value) => sum + value, 0);
  });

  return (
    <section
      aria-label={`Line items: ${table}`}
      aria-description={GRID_KEYBOARD_MAP}
      style={{ display: "grid", gap: "var(--soa-space-2)" }}
    >
      <div style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "center" }}>
        <Button size="sm" variant="secondary" isDisabled={readOnly} onPress={onAddRow}>
          Add row
        </Button>
        <Button size="sm" variant="subtle" isDisabled={!canUndo || readOnly} onPress={onUndo}>
          Undo
        </Button>
        <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
          {rows.length} row(s)
        </span>
      </div>

      <div
        data-testid={`grid-viewport-${table}`}
        onScroll={(event) => setScrollTop((event.target as HTMLElement).scrollTop)}
        style={{
          overflow: "auto",
          maxHeight: `${ROW_HEIGHT * VIEWPORT_ROWS}px`,
          border: "1px solid var(--soa-border)",
          borderRadius: "var(--soa-radius-panel)",
        }}
      >
        <table
          role="grid"
          aria-label={`${table} rows`}
          aria-rowcount={rows.length}
          style={{ borderCollapse: "collapse", width: "100%" }}
        >
          <thead>
            <tr>
              <th
                scope="col"
                style={{
                  position: "sticky",
                  left: 0,
                  top: 0,
                  zIndex: 2,
                  background: "var(--soa-surface)",
                  padding: "0.25rem 0.5rem",
                  textAlign: "left",
                }}
              >
                {columnLabel(columns[0])}
              </th>
              {columns.slice(1).map((key) => (
                <th
                  key={key}
                  scope="col"
                  style={{
                    position: "sticky",
                    top: 0,
                    background: "var(--soa-surface)",
                    padding: "0.25rem 0.5rem",
                    textAlign: "left",
                  }}
                >
                  {columnLabel(key)}
                </th>
              ))}
              <th scope="col" style={{ padding: "0.25rem 0.5rem" }}>
                row
              </th>
            </tr>
          </thead>
          <tbody>
            {start > 0 ? (
              <tr aria-hidden style={{ height: start * ROW_HEIGHT }}>
                <td colSpan={columns.length + 1} />
              </tr>
            ) : null}
            {visible.map((row) => (
              <tr key={row.rowIndex} aria-rowindex={row.rowIndex + 1}>
                {columns.map((key, columnIndex) => {
                  const id = cellId(key, row.rowIndex);
                  const cell = row.cells[key];
                  const save = saveStates[id];
                  return (
                    <td
                      key={key}
                      style={{
                        padding: "0.125rem 0.25rem",
                        borderBottom: "1px solid var(--soa-border)",
                        ...(columnIndex === 0
                          ? {
                              position: "sticky",
                              left: 0,
                              zIndex: 1,
                              background: "var(--soa-surface)",
                            }
                          : {}),
                      }}
                    >
                      <input
                        aria-label={`${columnLabel(key)} row ${row.rowIndex}`}
                        data-review-field-key={key}
                        data-review-row-index={row.rowIndex}
                        ref={(element) => {
                          if (element) inputs.current.set(id, element);
                          else inputs.current.delete(id);
                        }}
                        value={drafts[id] ?? cell?.value ?? ""}
                        readOnly={readOnly}
                        onFocus={() => onCellFocus(key, row.rowIndex)}
                        onChange={(event) =>
                          setDrafts((prev) => ({ ...prev, [id]: event.target.value }))
                        }
                        onBlur={() => {
                          if (skipNextBlur.current.delete(id)) return;
                          saveDraft(key, row.rowIndex);
                        }}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") {
                            event.preventDefault();
                            const attempted = saveDraft(key, row.rowIndex);
                            // Spreadsheet flow: down the same column.
                            const below = rows.find(
                              (candidate) => candidate.rowIndex > row.rowIndex,
                            );
                            if (below) {
                              if (attempted) skipNextBlur.current.add(id);
                              inputs.current.get(cellId(key, below.rowIndex))?.focus();
                            }
                          } else if (event.key === "Escape") {
                            setDrafts((prev) => ({ ...prev, [id]: cell?.value ?? "" }));
                          }
                        }}
                        onPaste={(event) => {
                          const text = event.clipboardData.getData("text/plain");
                          if (!text.includes("\t") || readOnly) return;
                          event.preventDefault();
                          const parts = text.split("\t");
                          columns.slice(columnIndex).forEach((targetKey, offset) => {
                            const part = parts[offset];
                            if (part !== undefined) {
                              onSaveCell(targetKey, row.rowIndex, part.trim());
                            }
                          });
                        }}
                        style={{
                          font: "inherit",
                          width: "100%",
                          minWidth: "5rem",
                          padding: "0.25rem 0.375rem",
                          border: cell?.corrected
                            ? "1.5px solid var(--soa-accent)"
                            : "1px solid var(--soa-border)",
                          borderRadius: "var(--soa-radius-control)",
                          background: "var(--soa-surface)",
                          color: "var(--soa-text-primary)",
                        }}
                      />
                      <span aria-live="polite" style={{ font: "var(--soa-font-caption)" }}>
                        {save?.status === "saving"
                          ? "Saving…"
                          : save?.status === "error"
                            ? `Not saved: ${save.message}`
                            : ""}
                      </span>
                      {cell?.normalizationError ? (
                        <span role="alert" style={{ font: "var(--soa-font-caption)" }}>
                          {cell.normalizationError}
                        </span>
                      ) : null}
                    </td>
                  );
                })}
                <td style={{ whiteSpace: "nowrap", padding: "0.125rem 0.25rem" }}>
                  <Button
                    size="sm"
                    variant="subtle"
                    isDisabled={readOnly}
                    aria-label={`Split row ${row.rowIndex}`}
                    onPress={() => onSplitRow(row.rowIndex)}
                  >
                    Split
                  </Button>
                  <Button
                    size="sm"
                    variant="subtle"
                    isDisabled={readOnly || row.rowIndex === rows[0]?.rowIndex}
                    aria-label={`Merge row ${row.rowIndex} into the row above`}
                    onPress={() => onMergeUp(row.rowIndex)}
                  >
                    Merge up
                  </Button>
                  <Button
                    size="sm"
                    variant="subtle"
                    isDisabled={readOnly}
                    aria-label={`Remove row ${row.rowIndex}`}
                    onPress={() => onRemoveRow(row.rowIndex)}
                  >
                    Remove
                  </Button>
                </td>
              </tr>
            ))}
            {end < rows.length ? (
              <tr aria-hidden style={{ height: (rows.length - end) * ROW_HEIGHT }}>
                <td colSpan={columns.length + 1} />
              </tr>
            ) : null}
          </tbody>
          <tfoot>
            <tr>
              {columns.map((key, index) => (
                <td
                  key={key}
                  style={{
                    padding: "0.25rem 0.5rem",
                    fontWeight: 600,
                    ...(index === 0
                      ? {
                          position: "sticky",
                          left: 0,
                          background: "var(--soa-surface)",
                        }
                      : {}),
                  }}
                >
                  {index === 0 ? (
                    "Totals"
                  ) : totals[index] !== null ? (
                    <Badge tone="neutral">{totals[index]?.toFixed(2)}</Badge>
                  ) : (
                    ""
                  )}
                </td>
              ))}
              <td />
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}
