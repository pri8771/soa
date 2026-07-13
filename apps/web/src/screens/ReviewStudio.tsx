/**
 * Review Studio (REV-007): the correction workspace for one task.
 *
 * Loads the REV-006 read model once, then: the document viewer with the
 * fields' evidence overlaid (focusing a field highlights its source;
 * clicking a source focuses its field), the header field editor with
 * autosave through the REV-009 corrections endpoint, and the live route
 * decision after each save. A stale-version conflict shows exactly what
 * happened and offers a reload — nothing is silently lost.
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useMemo, useRef, useState } from "react";

import {
  ApiError,
  correctField,
  fetchReviewWorkspace,
  type CorrectionResult,
  type ReviewWorkspace,
  type WorkspaceField,
} from "../api/client";
import { HeaderFieldEditor, type SaveState } from "../components/review/HeaderFieldEditor";
import { LineItemGrid, type GridRow } from "../components/review/LineItemGrid";
import { DocumentViewer, type EvidenceHighlight } from "../components/viewer/DocumentViewer";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

function evidenceId(fieldKey: string, index: number): string {
  return `${fieldKey}#${index}`;
}

//: Canonical column order for the lines table; unknown tables fall back
//: to the sorted union of their keys.
const LINE_COLUMNS = [
  "lines.sku",
  "lines.description",
  "lines.quantity",
  "lines.unit_price",
  "lines.line_total",
];

/** Effective grid rows: extraction cells overlaid by the latest
 * corrections, plus correction-only (added) rows; fully cleared rows are
 * treated as removed unless locally pending. Row identity is the stable
 * extraction/correction row_index. */
function buildGridRows(
  workspace: ReviewWorkspace,
  table: string,
  pendingRows: number[],
): { columns: string[]; rows: GridRow[] } {
  const byRow = new Map<number, GridRow>();
  const ensure = (rowIndex: number): GridRow => {
    const existing = byRow.get(rowIndex);
    if (existing) return existing;
    const created: GridRow = { rowIndex, cells: {} };
    byRow.set(rowIndex, created);
    return created;
  };
  for (const rowCells of workspace.line_items[table] ?? []) {
    for (const cell of rowCells) {
      if (cell.row_index === null) continue;
      ensure(cell.row_index).cells[cell.field_key] = {
        value: cell.raw_value,
        corrected: false,
        normalizationError: cell.normalization_error,
        hasEvidence: cell.evidence.length > 0,
      };
    }
  }
  for (const correction of workspace.corrections) {
    if (correction.row_index === null || !correction.field_key.startsWith(`${table}.`)) continue;
    ensure(correction.row_index).cells[correction.field_key] = {
      value: correction.corrected_raw_value,
      corrected: true,
      normalizationError: correction.normalization_error,
      hasEvidence: false,
    };
  }
  for (const rowIndex of pendingRows) ensure(rowIndex);
  const keys = new Set<string>();
  for (const row of byRow.values()) Object.keys(row.cells).forEach((key) => keys.add(key));
  const columns = table === "lines" ? LINE_COLUMNS : [...keys].sort();
  const rows = [...byRow.values()]
    .filter(
      (row) =>
        pendingRows.includes(row.rowIndex) ||
        Object.values(row.cells).some((cell) => cell.value !== null),
    )
    .sort((a, b) => a.rowIndex - b.rowIndex);
  return { columns, rows };
}

export function ReviewStudio() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const { taskId } = useParams({ strict: false }) as { taskId: string };
  const queryClient = useQueryClient();

  const workspace = useQuery({
    queryKey: ["review-workspace", slug, taskId],
    queryFn: () => fetchReviewWorkspace(slug, taskId),
  });

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saveStates, setSaveStates] = useState<Record<string, SaveState>>({});
  const [activeFieldKey, setActiveFieldKey] = useState<string | null>(null);

  const [conflict, setConflict] = useState<string | null>(null);
  const [decision, setDecision] = useState<{
    route: string;
    reasons: Record<string, unknown>[];
  } | null>(null);

  //: Grid state: locally added (still empty) rows and the undo stack —
  //: each entry restores a batch of previous cell values as NEW
  //: corrections, so history stays append-only.
  const [pendingRows, setPendingRows] = useState<number[]>([]);
  const [undoStack, setUndoStack] = useState<
    { fieldKey: string; rowIndex: number | null; previous: string | null }[][]
  >([]);
  //: Serialize saves: corrections chain the task version, so concurrent
  //: posts from one client would trip their own optimistic lock.
  const queue = useRef(Promise.resolve());
  const versionRef = useRef<number | null>(null);

  const stateKey = (fieldKey: string, rowIndex: number | null) =>
    rowIndex === null ? fieldKey : `${fieldKey}#${rowIndex}`;

  const save = useMutation({
    mutationFn: ({
      fieldKey,
      rowIndex,
      value,
    }: {
      fieldKey: string;
      rowIndex: number | null;
      value: string;
    }) =>
      correctField(slug, taskId, {
        field_key: fieldKey,
        row_index: rowIndex,
        value: value === "" ? null : value,
        expected_version: versionRef.current ?? workspace.data?.task.version ?? 0,
      }),
    onMutate: ({ fieldKey, rowIndex }) =>
      setSaveStates((prev) => ({ ...prev, [stateKey(fieldKey, rowIndex)]: { status: "saving" } })),
    onSuccess: (result: CorrectionResult, { fieldKey, rowIndex }) => {
      versionRef.current = result.task_version;
      setSaveStates((prev) => ({ ...prev, [stateKey(fieldKey, rowIndex)]: { status: "saved" } }));
      if (result.revalidation) setDecision(result.revalidation.decision);
      void queryClient.invalidateQueries({ queryKey: ["review-workspace", slug, taskId] });
    },
    onError: (error: unknown, { fieldKey, rowIndex }) => {
      const message = error instanceof Error ? error.message : "The change was not saved.";
      if (error instanceof ApiError && error.status === 409) {
        setConflict(message);
      }
      setSaveStates((prev) => ({
        ...prev,
        [stateKey(fieldKey, rowIndex)]: { status: "error", message },
      }));
    },
  });

  const enqueueSave = (fieldKey: string, rowIndex: number | null, value: string) => {
    queue.current = queue.current.then(() =>
      save.mutateAsync({ fieldKey, rowIndex, value }).then(
        () => undefined,
        () => undefined,
      ),
    );
    return queue.current;
  };

  const saveBatch = (
    edits: { fieldKey: string; rowIndex: number; value: string }[],
    undoEntries: { fieldKey: string; rowIndex: number | null; previous: string | null }[],
  ) => {
    setUndoStack((prev) => [...prev, undoEntries]);
    for (const edit of edits) void enqueueSave(edit.fieldKey, edit.rowIndex, edit.value);
  };

  const headerFields: WorkspaceField[] = workspace.data?.fields ?? [];
  const evidence: EvidenceHighlight[] = useMemo(
    () =>
      headerFields.flatMap((field) =>
        field.evidence.map((span, index) => ({
          id: evidenceId(field.field_key, index),
          label: field.field_key,
          page_number: span.page_number,
          polygon: span.certainty === "region" ? span.polygon : null,
          kind: (field.field_key === activeFieldKey ? "active" : "related") as "active" | "related",
        })),
      ),
    [headerFields, activeFieldKey],
  );
  const activeEvidenceId = useMemo(() => {
    if (activeFieldKey === null) return null;
    const field = headerFields.find((entry) => entry.field_key === activeFieldKey);
    return field && field.evidence.length > 0 ? evidenceId(field.field_key, 0) : null;
  }, [activeFieldKey, headerFields]);

  if (workspace.status === "pending") {
    return (
      <AppShell title="Review" breadcrumbs={[{ label: session.organization.name }]}>
        <Skeleton height="20rem" />
      </AppShell>
    );
  }
  if (workspace.status === "error") {
    return (
      <AppShell title="Review" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load the review workspace"
          action={
            <Button size="sm" onPress={() => void workspace.refetch()}>
              Try again
            </Button>
          }
        >
          It may have been removed, or the service did not respond.
        </Banner>
      </AppShell>
    );
  }

  const data = workspace.data;
  const me = `user:${session.userId}`;
  const editable = data.task.state === "in_progress" && data.task.assigned_to === me;
  const currentDecision = decision ?? data.run.decision;

  return (
    <AppShell
      title={`Review: ${data.document.original_filename}`}
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Review", to: "/app/$organizationSlug/review" },
        { label: data.document.original_filename },
      ]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
        {conflict ? (
          <Banner
            tone="critical"
            title="Someone else changed this task"
            action={
              <Button
                size="sm"
                onPress={() => {
                  setConflict(null);
                  setDrafts({});
                  setSaveStates({});
                  versionRef.current = null;
                  void workspace.refetch();
                }}
              >
                Reload the workspace
              </Button>
            }
          >
            {conflict}
          </Banner>
        ) : null}
        {!editable ? (
          <Banner tone="info" title="Read-only">
            {data.task.state === "in_progress"
              ? `This task is assigned to ${data.task.assigned_to}; claim it from the queue to edit.`
              : `This task is ${data.task.state}; claim it from the queue to edit.`}
          </Banner>
        ) : null}
        {currentDecision ? (
          <div style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "center" }}>
            <span style={{ font: "var(--soa-font-caption)" }}>Current decision:</span>
            <Badge tone={currentDecision.route === "approved" ? "success" : "warning"}>
              {currentDecision.route.replace(/_/g, " ")}
            </Badge>
            <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
              {currentDecision.reasons.length === 0
                ? "no open reasons"
                : `${currentDecision.reasons.length} reason(s) remain`}
            </span>
          </div>
        ) : null}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(0, 3fr) minmax(20rem, 2fr)",
            gap: "var(--soa-space-4)",
            alignItems: "start",
          }}
        >
          <DocumentViewer
            organizationSlug={slug}
            documentId={data.document.id}
            evidence={evidence}
            activeEvidenceId={activeEvidenceId}
            onEvidenceSelect={(id) => setActiveFieldKey(id.split("#")[0])}
          />
          <HeaderFieldEditor
            fields={headerFields}
            reasons={data.task.reasons}
            drafts={drafts}
            saveStates={saveStates}
            activeFieldKey={activeFieldKey}
            onFieldFocus={setActiveFieldKey}
            onDraftChange={(fieldKey, value) =>
              setDrafts((prev) => ({ ...prev, [fieldKey]: value }))
            }
            onSave={(fieldKey, value) => void enqueueSave(fieldKey, null, value)}
            readOnly={!editable}
          />
        </div>

        {Object.keys({ ...data.line_items, ...(pendingRows.length ? { lines: [] } : {}) }).map(
          (table) => {
            const { columns, rows } = buildGridRows(data, table, pendingRows);
            const nextRowIndex = rows.reduce((max, row) => Math.max(max, row.rowIndex + 1), 0);
            const effective = (fieldKey: string, rowIndex: number) =>
              rows.find((row) => row.rowIndex === rowIndex)?.cells[fieldKey]?.value ?? null;
            return (
              <LineItemGrid
                key={table}
                table={table}
                columns={columns}
                rows={rows}
                saveStates={saveStates}
                readOnly={!editable}
                onCellFocus={(fieldKey) => setActiveFieldKey(fieldKey)}
                onSaveCell={(fieldKey, rowIndex, value) => {
                  setUndoStack((prev) => [
                    ...prev,
                    [{ fieldKey, rowIndex, previous: effective(fieldKey, rowIndex) }],
                  ]);
                  void enqueueSave(fieldKey, rowIndex, value);
                }}
                onAddRow={() => setPendingRows((prev) => [...prev, nextRowIndex])}
                onRemoveRow={(rowIndex) => {
                  const row = rows.find((candidate) => candidate.rowIndex === rowIndex);
                  if (!row) return;
                  const filled = columns.filter((key) => row.cells[key]?.value != null);
                  saveBatch(
                    filled.map((key) => ({ fieldKey: key, rowIndex, value: "" })),
                    filled.map((key) => ({
                      fieldKey: key,
                      rowIndex,
                      previous: row.cells[key]?.value ?? null,
                    })),
                  );
                  setPendingRows((prev) => prev.filter((index) => index !== rowIndex));
                }}
                onSplitRow={(rowIndex) => {
                  const row = rows.find((candidate) => candidate.rowIndex === rowIndex);
                  if (!row) return;
                  const filled = columns.filter((key) => row.cells[key]?.value != null);
                  saveBatch(
                    filled.map((key) => ({
                      fieldKey: key,
                      rowIndex: nextRowIndex,
                      value: row.cells[key]?.value ?? "",
                    })),
                    filled.map((key) => ({
                      fieldKey: key,
                      rowIndex: nextRowIndex,
                      previous: null,
                    })),
                  );
                }}
                onMergeUp={(rowIndex) => {
                  const position = rows.findIndex((candidate) => candidate.rowIndex === rowIndex);
                  const source = rows[position];
                  const above = rows[position - 1];
                  if (!source || !above) return;
                  const numeric = (value: string | null) => {
                    const parsed = Number((value ?? "").replace(/,/g, ""));
                    return Number.isFinite(parsed) ? parsed : null;
                  };
                  const edits: { fieldKey: string; rowIndex: number; value: string }[] = [];
                  const undoEntries: {
                    fieldKey: string;
                    rowIndex: number | null;
                    previous: string | null;
                  }[] = [];
                  for (const key of [`${table}.quantity`, `${table}.line_total`]) {
                    const a = numeric(above.cells[key]?.value ?? null);
                    const b = numeric(source.cells[key]?.value ?? null);
                    if (a !== null && b !== null) {
                      edits.push({
                        fieldKey: key,
                        rowIndex: above.rowIndex,
                        value: String(a + b),
                      });
                      undoEntries.push({
                        fieldKey: key,
                        rowIndex: above.rowIndex,
                        previous: above.cells[key]?.value ?? null,
                      });
                    }
                  }
                  for (const key of columns) {
                    if (source.cells[key]?.value != null) {
                      edits.push({ fieldKey: key, rowIndex: source.rowIndex, value: "" });
                      undoEntries.push({
                        fieldKey: key,
                        rowIndex: source.rowIndex,
                        previous: source.cells[key]?.value ?? null,
                      });
                    }
                  }
                  saveBatch(edits, undoEntries);
                }}
                canUndo={undoStack.length > 0}
                onUndo={() => {
                  const entries = undoStack[undoStack.length - 1];
                  if (!entries) return;
                  setUndoStack((prev) => prev.slice(0, -1));
                  for (const entry of entries) {
                    void enqueueSave(entry.fieldKey, entry.rowIndex, entry.previous ?? "");
                  }
                }}
              />
            );
          },
        )}
      </div>
    </AppShell>
  );
}
