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
import { useNavigate, useParams } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  approveReviewTask,
  claimReviewTask,
  correctField,
  escalateReviewTask,
  fetchCatalogCandidates,
  fetchReviewWorkspace,
  locateFieldValue,
  postCatalogSelection,
  rejectReviewTask,
  type CatalogSelectionResult,
  type CorrectionResult,
  type EvidenceSelection,
  type ReviewWorkspace,
  type WorkspaceField,
} from "../api/client";
import { ApprovalPanel } from "../components/review/ApprovalPanel";
import {
  CatalogCandidatePicker,
  type CatalogCandidate,
} from "../components/review/CatalogCandidatePicker";
import { ConflictResolver, type ConflictEntry } from "../components/review/ConflictResolver";
import { HeaderFieldEditor, type SaveState } from "../components/review/HeaderFieldEditor";
import { SplitLayout } from "../components/review/SplitLayout";
import { LineItemGrid, type GridRow } from "../components/review/LineItemGrid";
import { ReviewComments } from "../components/review/ReviewComments";
import { DocumentViewer, type EvidenceHighlight } from "../components/viewer/DocumentViewer";
import { boundingArea } from "../components/viewer/geometry";
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

//: Fields the catalog matches (CAT-010) and how the picker labels them.
//: The server owns the field-type mapping; this only decides when the
//: picker is offered.
const CATALOG_FIELD_LABELS: Record<string, string> = {
  customer_name: "customer",
  "lines.sku": "SKU",
};

const REVIEW_SHORTCUTS = [
  ["J / K", "Next / previous review reason"],
  ["E", "Focus the active field’s source evidence"],
  ["M", "Focus catalog match candidates for the active field"],
  ["C", "Add a discussion comment"],
  ["A", "Open the approval summary when eligible"],
  ["R", "Open rejection, or escalation when rejection is unavailable"],
  ["?", "Show or hide this keyboard guide"],
] as const;

function isTypingContext(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]') !== null
  );
}

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
  const navigate = useNavigate();

  const workspace = useQuery({
    queryKey: ["review-workspace", slug, taskId],
    queryFn: () => fetchReviewWorkspace(slug, taskId),
  });

  //: A reprocess supersedes this task and opens a fresh one for the new
  //: run; a bookmarked or reused URL then points at dead data. When the
  //: server names the document's current active task, redirect to it so
  //: the reviewer always lands on the live review.
  const supersededBy = workspace.data?.superseded_by_task_id ?? null;
  useEffect(() => {
    if (supersededBy && supersededBy !== taskId) {
      void navigate({
        to: "/app/$organizationSlug/review/$taskId",
        params: { organizationSlug: slug, taskId: supersededBy },
        replace: true,
      });
    }
  }, [supersededBy, taskId, slug, navigate]);

  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saveStates, setSaveStates] = useState<Record<string, SaveState>>({});
  const [activeFieldKey, setActiveFieldKey] = useState<string | null>(null);
  //: Which row the active field belongs to (null = header) so a catalog
  //: pick lands on the right cell.
  const [activeRowIndex, setActiveRowIndex] = useState<number | null>(null);
  const [catalogMessage, setCatalogMessage] = useState<string | null>(null);
  //: The search text behind the currently shown candidates — the
  //: selection posts it so the server can recompute the same decision.
  const catalogQueryRef = useRef("");

  const [conflict, setConflict] = useState<string | null>(null);
  //: Edits refused by a version conflict — HELD (not lost) until the
  //: reviewer chooses per field: keep mine (merge) or take the server's.
  const [conflictEdits, setConflictEdits] = useState<
    { fieldKey: string; rowIndex: number | null; value: string }[]
  >([]);
  const [decision, setDecision] = useState<{
    route: string;
    reasons: Record<string, unknown>[];
  } | null>(null);
  //: Whether CRITICAL blockers remain — the routing verdict until a
  //: correction revalidates, then the fresh evaluation's flag.
  const [revalidatedBlocking, setRevalidatedBlocking] = useState<boolean | null>(null);
  const [completionMessage, setCompletionMessage] = useState<string | null>(null);
  //: Counts work as soon as it enters the serialized queue, rather than
  //: only once React Query starts the network mutation. This closes the
  //: edit-blur -> approve race for both single edits and row batches.
  const [pendingWrites, setPendingWrites] = useState(0);
  const [shortcutMessage, setShortcutMessage] = useState<string | null>(null);
  const [showKeyboardGuide, setShowKeyboardGuide] = useState(false);
  const shortcutReasonIndex = useRef(-1);

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
  //: Fields with a held conflict belong to the RESOLVER: the editor's
  //: blur/Enter autosaves are suppressed for them so a failed value can
  //: never sneak back in behind the reviewer's choice. A ref (not
  //: state) so the guard is correct in the same tick as the 409.
  const conflictKeysRef = useRef(new Set<string>());

  const stateKey = (fieldKey: string, rowIndex: number | null) =>
    rowIndex === null ? fieldKey : `${fieldKey}#${rowIndex}`;

  const focusField = (fieldKey: string, rowIndex: number | null = null) => {
    setActiveFieldKey(fieldKey);
    setActiveRowIndex(rowIndex);
    setCatalogMessage(null);
  };

  const save = useMutation({
    mutationFn: async ({
      fieldKey,
      rowIndex,
      value,
      region,
    }: {
      fieldKey: string;
      rowIndex: number | null;
      value: string;
      // A region drawn/adjusted by hand on the viewer. When present it is
      // authoritative and auto-locate is skipped.
      region?: EvidenceSelection;
    }) => {
      let evidenceSelection: EvidenceSelection | undefined = region;
      const trimmed = value.trim();
      // Auto-locate a typed value only when no hand-drawn region is given —
      // best-effort, a miss/error never blocks the save.
      if (evidenceSelection === undefined && trimmed !== "") {
        try {
          const located = await locateFieldValue(slug, taskId, trimmed);
          if (located.found && located.page_number != null && located.polygon) {
            evidenceSelection = {
              page_number: located.page_number,
              polygon: located.polygon,
              quote: trimmed,
            };
          }
        } catch {
          // ignore — locating is a convenience, not a precondition of saving
        }
      }
      return correctField(slug, taskId, {
        field_key: fieldKey,
        row_index: rowIndex,
        value: value === "" ? null : value,
        expected_version: versionRef.current ?? workspace.data?.task.version ?? 0,
        evidence_selection: evidenceSelection ?? null,
      });
    },
    onMutate: ({ fieldKey, rowIndex }) =>
      setSaveStates((prev) => ({ ...prev, [stateKey(fieldKey, rowIndex)]: { status: "saving" } })),
    onSuccess: (result: CorrectionResult, { fieldKey, rowIndex }) => {
      versionRef.current = result.task_version;
      setSaveStates((prev) => ({ ...prev, [stateKey(fieldKey, rowIndex)]: { status: "saved" } }));
      if (result.revalidation) {
        setDecision(result.revalidation.decision);
        setRevalidatedBlocking(Boolean(result.revalidation.evaluation["blocking"]));
      }
      void queryClient.invalidateQueries({ queryKey: ["review-workspace", slug, taskId] });
    },
    onError: (error: unknown, { fieldKey, rowIndex, value }) => {
      const message = error instanceof Error ? error.message : "The change was not saved.";
      if (error instanceof ApiError && error.status === 409) {
        setConflict(message);
        // Hold the refused edit and refresh so the resolver can show
        // the server's value, its editor, and the fresh task version.
        setConflictEdits((prev) => [
          ...prev.filter((entry) => !(entry.fieldKey === fieldKey && entry.rowIndex === rowIndex)),
          { fieldKey, rowIndex, value },
        ]);
        conflictKeysRef.current.add(stateKey(fieldKey, rowIndex));
        versionRef.current = null;
        void workspace.refetch();
      }
      setSaveStates((prev) => ({
        ...prev,
        [stateKey(fieldKey, rowIndex)]: { status: "error", message },
      }));
    },
  });

  // Claim the task right here so a reviewer can go from opening a link to
  // editing without a detour through the queue. Only an OPEN task is
  // claimable; the queue owns contention (a lost race surfaces as a 409).
  const claim = useMutation({
    mutationFn: () => claimReviewTask(slug, taskId),
    onSuccess: () => {
      versionRef.current = null;
      void queryClient.invalidateQueries({ queryKey: ["review-workspace", slug, taskId] });
    },
  });

  const enqueueSave = (fieldKey: string, rowIndex: number | null, value: string) => {
    // A conflicted field saves only through the resolver's explicit
    // choice — autosaves (blur/Enter) must not race the decision.
    if (conflictKeysRef.current.has(stateKey(fieldKey, rowIndex))) return queue.current;
    setPendingWrites((count) => count + 1);
    queue.current = queue.current.then(async () => {
      try {
        await save.mutateAsync({ fieldKey, rowIndex, value });
      } catch {
        // The mutation owns the visible field-level error. Keep the queue
        // fulfilled so a corrected/retried value can run behind it.
      } finally {
        setPendingWrites((count) => Math.max(0, count - 1));
      }
    });
    return queue.current;
  };

  //: A catalog pick (CAT-010) is a correction authored by the matcher:
  //: it chains the task version like any save and comes back with the
  //: fresh revalidation.
  const pickCatalog = useMutation({
    mutationFn: ({
      fieldKey,
      rowIndex,
      candidate,
      reason,
    }: {
      fieldKey: string;
      rowIndex: number | null;
      candidate: CatalogCandidate;
      reason: string | null;
    }) =>
      postCatalogSelection(slug, taskId, {
        field_key: fieldKey,
        row_index: rowIndex,
        query: catalogQueryRef.current,
        selected_source_id: candidate.code,
        ...(reason === null ? {} : { reason }),
        expected_version: versionRef.current ?? workspace.data?.task.version ?? 0,
      }),
    onSuccess: (result: CatalogSelectionResult) => {
      versionRef.current = result.task_version;
      setCatalogMessage(
        result.correction
          ? `Matched: the field is now “${result.correction.corrected_raw_value ?? ""}”` +
              (result.override ? " (manual override recorded)." : ".")
          : "Recorded.",
      );
      if (result.revalidation) {
        setDecision(result.revalidation.decision);
        setRevalidatedBlocking(Boolean(result.revalidation.evaluation["blocking"]));
      }
      void queryClient.invalidateQueries({ queryKey: ["review-workspace", slug, taskId] });
    },
    onError: (error: unknown) =>
      setCatalogMessage(
        error instanceof Error
          ? `The pick was not saved: ${error.message}`
          : "The pick was not saved.",
      ),
  });
  const enqueuePick = (candidate: CatalogCandidate, reason: string | null) => {
    if (activeFieldKey === null) return;
    const fieldKey = activeFieldKey;
    const rowIndex = activeRowIndex;
    setPendingWrites((count) => count + 1);
    queue.current = queue.current.then(async () => {
      try {
        await pickCatalog.mutateAsync({ fieldKey, rowIndex, candidate, reason });
      } catch {
        // CatalogCandidatePicker owns the visible failure state.
      } finally {
        setPendingWrites((count) => Math.max(0, count - 1));
      }
    });
  };

  const saveBatch = (
    edits: { fieldKey: string; rowIndex: number; value: string }[],
    undoEntries: { fieldKey: string; rowIndex: number | null; previous: string | null }[],
  ) => {
    setUndoStack((prev) => [...prev, undoEntries]);
    for (const edit of edits) void enqueueSave(edit.fieldKey, edit.rowIndex, edit.value);
  };

  //: Completion actions (REV-013). Each refreshes the workspace so the
  //: task state, decision, and read-only banner reflect the outcome, and
  //: invalidates the queues and dashboard this task appears on so they
  //: don't show it as still open.
  const finishRefresh = () => {
    versionRef.current = null;
    void queryClient.invalidateQueries({ queryKey: ["review-workspace", slug, taskId] });
    void queryClient.invalidateQueries({ queryKey: ["review-tasks", slug] });
    void queryClient.invalidateQueries({ queryKey: ["documents", slug] });
    void queryClient.invalidateQueries({ queryKey: ["operations", slug] });
  };
  const failure = (verb: string) => (error: unknown) =>
    setCompletionMessage(
      error instanceof Error ? `${verb} failed: ${error.message}` : `${verb} failed.`,
    );
  const approve = useMutation({
    mutationFn: (overrideReason: string | null) =>
      approveReviewTask(slug, taskId, overrideReason ?? undefined),
    onSuccess: (result) => {
      setCompletionMessage(
        result.status === "pending_second_approval"
          ? "First approval recorded — a second approver must finish this task."
          : result.override_used
            ? "Order approved with an authorized override."
            : "Order approved.",
      );
      finishRefresh();
    },
    onError: failure("Approval"),
  });
  const reject = useMutation({
    mutationFn: (reason: string) => rejectReviewTask(slug, taskId, reason),
    onSuccess: () => {
      setCompletionMessage("Document rejected.");
      finishRefresh();
    },
    onError: failure("Rejection"),
  });
  const escalate = useMutation({
    mutationFn: (reason: string) => escalateReviewTask(slug, taskId, reason),
    onSuccess: () => {
      setCompletionMessage("Task escalated and returned to the queue at top priority.");
      finishRefresh();
    },
    onError: failure("Escalation"),
  });

  const headerFields: WorkspaceField[] = useMemo(
    () => workspace.data?.fields ?? [],
    [workspace.data?.fields],
  );
  // Tighten the active field's highlight. The model's stored evidence box
  // is the bounding box of its verbatim quote, and a rambling quote (e.g.
  // currency "GBP" quoted with the whole cost paragraph around it) boxes
  // half the page. Re-anchor the highlight to the field's actual value so
  // clicking a field marks JUST the value, not the paragraph it sat in.
  // locate() never mutates, so this works on a read-only (unclaimed) task.
  const activeField = useMemo(
    () => headerFields.find((entry) => entry.field_key === activeFieldKey) ?? null,
    [headerFields, activeFieldKey],
  );
  const activeValue = (
    drafts[stateKey(activeFieldKey ?? "", activeRowIndex)] ??
    activeField?.raw_value ??
    ""
  ).trim();
  const activePageHint = activeField?.evidence[0]?.page_number;
  const locatedActive = useQuery({
    queryKey: ["locate", slug, taskId, activeFieldKey, activeRowIndex, activeValue],
    queryFn: () => locateFieldValue(slug, taskId, activeValue, activePageHint),
    enabled: activeFieldKey !== null && activeValue !== "",
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const located = locatedActive.data;
  const evidence: EvidenceHighlight[] = useMemo(() => {
    const tightPolygon =
      located?.found === true && located.polygon != null && located.polygon.length > 0
        ? (located.polygon as [number, number][])
        : null;
    const tightPage = located?.found === true ? (located.page_number ?? null) : null;
    return headerFields.flatMap((field) => {
      const isActive = field.field_key === activeFieldKey;
      return field.evidence.map((span, index) => {
        const stored = span.certainty === "region" ? span.polygon : null;
        // Re-anchor the active field's highlight to the located value box, but
        // only when it is a genuine improvement: same page as the model's
        // evidence (never strand the highlight on another page), and either
        // the model gave no box or its box is materially larger than the
        // located one — so we never overwrite an already-tight, correct box
        // with a possibly-different occurrence of the value.
        const canTighten =
          isActive &&
          index === 0 &&
          tightPolygon !== null &&
          tightPage === span.page_number &&
          (stored === null || boundingArea(tightPolygon) < boundingArea(stored) * 0.5);
        // A field the reviewer has cleared has no value to point at — drop the
        // stale (often over-broad) box rather than leaving it highlighted.
        const cleared = isActive && index === 0 && activeValue === "";
        return {
          id: evidenceId(field.field_key, index),
          label: field.field_key,
          page_number: canTighten && tightPage !== null ? tightPage : span.page_number,
          polygon: cleared ? null : canTighten ? tightPolygon : stored,
          kind: (isActive ? "active" : "related") as "active" | "related",
        };
      });
    });
  }, [headerFields, activeFieldKey, activeValue, located]);
  const activeEvidenceId = useMemo(() => {
    if (activeFieldKey === null) return null;
    const field = headerFields.find((entry) => entry.field_key === activeFieldKey);
    return field && field.evidence.length > 0 ? evidenceId(field.field_key, 0) : null;
  }, [activeFieldKey, headerFields]);

  // A region drawn or adjusted on the viewer saves against the active
  // field, preserving its current value — so a reviewer can teach WHERE a
  // (blank or filled) field lives on the page.
  const onRegionDrawn = useCallback(
    ({ pageNumber, polygon }: { pageNumber: number; polygon: number[][] }) => {
      if (activeFieldKey === null) return;
      const field = headerFields.find((entry) => entry.field_key === activeFieldKey);
      const draftValue = drafts[stateKey(activeFieldKey, activeRowIndex)];
      const value = draftValue ?? field?.raw_value ?? "";
      save.mutate({
        fieldKey: activeFieldKey,
        rowIndex: activeRowIndex,
        value,
        region: {
          page_number: pageNumber,
          polygon,
          quote: value.trim() === "" ? null : value.trim(),
        },
      });
    },
    [activeFieldKey, activeRowIndex, headerFields, drafts, save],
  );

  // Required Review Studio shortcuts are global within this screen, but
  // never consume keystrokes from an input, textarea, select, or editable
  // surface. Every shortcut moves focus or opens a real on-screen control.
  useEffect(() => {
    const onShortcut = (event: KeyboardEvent) => {
      if (
        event.defaultPrevented ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        isTypingContext(event.target)
      ) {
        return;
      }
      const key = event.key.toLowerCase();

      if (key === "j" || key === "k") {
        event.preventDefault();
        const reasons = (workspace.data?.task.reasons ?? []).filter(
          (reason) => reason.field_key !== null,
        );
        if (reasons.length === 0) {
          setShortcutMessage("There are no field-level review reasons to navigate.");
          return;
        }
        const delta = key === "j" ? 1 : -1;
        const current = shortcutReasonIndex.current;
        const next =
          current < 0
            ? delta > 0
              ? 0
              : reasons.length - 1
            : (current + delta + reasons.length) % reasons.length;
        shortcutReasonIndex.current = next;
        const reason = reasons[next];
        if (!reason?.field_key) return;
        focusField(reason.field_key, reason.row_index);
        setShortcutMessage(`Review reason ${next + 1} of ${reasons.length}: ${reason.message}`);
        window.setTimeout(() => {
          const row = reason.row_index === null ? "header" : String(reason.row_index);
          const field = Array.from(
            document.querySelectorAll<HTMLElement>("[data-review-field-key]"),
          ).find(
            (candidate) =>
              candidate.dataset.reviewFieldKey === reason.field_key &&
              candidate.dataset.reviewRowIndex === row,
          );
          field?.focus();
        }, 0);
        return;
      }

      if (key === "e") {
        event.preventDefault();
        if (activeEvidenceId === null) {
          setShortcutMessage("The active field has no source evidence to focus.");
          return;
        }
        const source = Array.from(
          document.querySelectorAll<HTMLElement>("[data-evidence-id]"),
        ).find((candidate) => candidate.dataset.evidenceId === activeEvidenceId);
        if (source) {
          source.focus();
          source.scrollIntoView?.({ block: "nearest", inline: "nearest" });
          setShortcutMessage("Source evidence focused.");
        } else {
          setShortcutMessage(
            "Source evidence is still loading. Press E again when the page appears.",
          );
        }
        return;
      }

      if (key === "m") {
        event.preventDefault();
        if (activeFieldKey === null || !(activeFieldKey in CATALOG_FIELD_LABELS)) {
          setShortcutMessage("Catalog matching is not available for the active field.");
          return;
        }
        const search = document.querySelector<HTMLInputElement>("[data-catalog-picker] input");
        if (search) {
          search.focus();
          search.scrollIntoView?.({ block: "nearest" });
          setShortcutMessage("Catalog match search focused.");
        }
        return;
      }

      if (key === "c") {
        event.preventDefault();
        const comment = document.querySelector<HTMLTextAreaElement>("[data-review-comment-input]");
        comment?.focus();
        comment?.scrollIntoView?.({ block: "nearest" });
        setShortcutMessage("Discussion comment editor focused.");
        return;
      }

      if (key === "a") {
        event.preventDefault();
        const approve = document.querySelector<HTMLButtonElement>('[data-review-action="approve"]');
        if (approve && !approve.disabled) {
          approve.click();
          approve.focus();
          setShortcutMessage("Approval summary opened. Review it before confirming.");
        } else {
          setShortcutMessage("Approval is not available. The reason is shown with the action.");
        }
        return;
      }

      if (key === "r") {
        event.preventDefault();
        const reject = document.querySelector<HTMLButtonElement>('[data-review-action="reject"]');
        const escalate = document.querySelector<HTMLButtonElement>(
          '[data-review-action="escalate"]',
        );
        const action = reject && !reject.disabled ? reject : escalate;
        if (action && !action.disabled) {
          action.click();
          action.focus();
          setShortcutMessage(
            action === reject ? "Rejection form opened." : "Escalation form opened.",
          );
        }
        return;
      }

      if (event.key === "?") {
        event.preventDefault();
        setShowKeyboardGuide((visible) => !visible);
        return;
      }

      if (key === "escape" && showKeyboardGuide) {
        event.preventDefault();
        setShowKeyboardGuide(false);
      }
    };

    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, [activeEvidenceId, activeFieldKey, showKeyboardGuide, workspace.data?.task.reasons]);

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
  const readOnlyReason =
    data.task.state === "in_progress"
      ? `This task is assigned to ${data.task.assigned_to}; claim it from the queue to edit.`
      : data.task.state === "open"
        ? "Claim this task to edit fields, locate values, and draw evidence regions."
        : `This task is ${data.task.state} and can no longer be edited.`;

  //: REV-014: the server's current value (and its editor, when a
  //: correction authored it) for a conflicted field.
  const serverValueFor = (
    fieldKey: string,
    rowIndex: number | null,
  ): { value: string | null; editor: string | null } => {
    const correction = data.corrections.find(
      (entry) => entry.field_key === fieldKey && entry.row_index === rowIndex,
    );
    if (correction) {
      return { value: correction.corrected_raw_value, editor: correction.corrected_by };
    }
    if (rowIndex === null) {
      const field = data.fields.find((entry) => entry.field_key === fieldKey);
      return { value: field?.raw_value ?? null, editor: null };
    }
    const table = fieldKey.split(".")[0] ?? fieldKey;
    for (const rowCells of data.line_items[table] ?? []) {
      for (const cell of rowCells) {
        if (cell.field_key === fieldKey && cell.row_index === rowIndex) {
          return { value: cell.raw_value, editor: null };
        }
      }
    }
    return { value: null, editor: null };
  };
  const conflictEntries: ConflictEntry[] = conflictEdits.map((edit) => {
    const server = serverValueFor(edit.fieldKey, edit.rowIndex);
    return {
      fieldKey: edit.fieldKey,
      rowIndex: edit.rowIndex,
      yourValue: edit.value,
      serverValue: server.value,
      serverEditor: server.editor,
    };
  });
  const dropConflictEdit = (entry: ConflictEntry) => {
    conflictKeysRef.current.delete(stateKey(entry.fieldKey, entry.rowIndex));
    setConflictEdits((prev) => {
      const next = prev.filter(
        (edit) => !(edit.fieldKey === entry.fieldKey && edit.rowIndex === entry.rowIndex),
      );
      if (next.length === 0) setConflict(null);
      return next;
    });
  };
  const taskStatus =
    `${data.task.state.replace(/_/g, " ")}` +
    (data.task.assigned_to === me
      ? " — assigned to you"
      : data.task.assigned_to
        ? ` — assigned to ${data.task.assigned_to}`
        : "") +
    ` (version ${data.task.version})`;
  const failedSaveCount = Object.values(saveStates).filter(
    (state) => state.status === "error",
  ).length;
  const approvalBlockedReason =
    pendingWrites > 0
      ? `Wait for ${pendingWrites} queued or saving change(s) before approving.`
      : failedSaveCount > 0
        ? `${failedSaveCount} change(s) failed to save. Retry them successfully before approving.`
        : null;

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
        <div style={{ display: "flex", alignItems: "center", gap: "var(--soa-space-2)" }}>
          <Button
            size="sm"
            variant="subtle"
            onPress={() => setShowKeyboardGuide((shown) => !shown)}
          >
            Keyboard shortcuts (?)
          </Button>
          <span role="status" aria-live="polite" style={{ font: "var(--soa-font-caption)" }}>
            {shortcutMessage}
          </span>
        </div>
        {showKeyboardGuide ? (
          <section
            role="dialog"
            aria-modal="false"
            aria-labelledby="review-shortcuts-heading"
            style={{
              border: "1px solid var(--soa-border)",
              borderRadius: "var(--soa-radius-panel)",
              padding: "var(--soa-space-3)",
              display: "grid",
              gap: "var(--soa-space-2)",
            }}
          >
            <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem" }}>
              <h2
                id="review-shortcuts-heading"
                style={{ font: "var(--soa-font-heading-sm)", margin: 0 }}
              >
                Review keyboard shortcuts
              </h2>
              <Button size="sm" variant="subtle" onPress={() => setShowKeyboardGuide(false)}>
                Close guide
              </Button>
            </div>
            <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
              Shortcuts pause while you type in a field, search, comment, or reason box.
            </p>
            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "max-content 1fr",
                gap: "0.375rem 1rem",
                margin: 0,
              }}
            >
              {REVIEW_SHORTCUTS.map(([keys, description]) => (
                <div key={keys} style={{ display: "contents" }}>
                  <dt>
                    <kbd>{keys}</kbd>
                  </dt>
                  <dd style={{ margin: 0 }}>{description}</dd>
                </div>
              ))}
            </dl>
          </section>
        ) : null}
        {conflict ? (
          <ConflictResolver
            message={conflict}
            conflicts={conflictEntries}
            taskStatus={taskStatus}
            onKeepMine={(entry) => {
              // Merge: re-save my value against the FRESH task version.
              dropConflictEdit(entry);
              void enqueueSave(entry.fieldKey, entry.rowIndex, entry.yourValue);
            }}
            onTakeServer={(entry) => {
              dropConflictEdit(entry);
              if (entry.rowIndex === null) {
                setDrafts((prev) => ({ ...prev, [entry.fieldKey]: entry.serverValue ?? "" }));
              }
              setSaveStates((prev) => {
                const next = { ...prev };
                delete next[stateKey(entry.fieldKey, entry.rowIndex)];
                return next;
              });
            }}
            onReloadDiscardingAll={() => {
              conflictKeysRef.current.clear();
              setConflict(null);
              setConflictEdits([]);
              setDrafts({});
              setSaveStates({});
              versionRef.current = null;
              void workspace.refetch();
            }}
          />
        ) : null}
        {!editable && data.task.state !== "completed" ? (
          <Banner
            tone="info"
            title="Read-only"
            action={
              data.task.state === "open" ? (
                <Button size="sm" onPress={() => claim.mutate()} isDisabled={claim.isPending}>
                  {claim.isPending ? "Claiming…" : "Claim to edit"}
                </Button>
              ) : undefined
            }
          >
            {claim.isError
              ? claim.error instanceof Error
                ? claim.error.message
                : "Couldn’t claim the task."
              : readOnlyReason}
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

        <SplitLayout
          storageKey="soa.review.split"
          leftLabel="Fields"
          rightLabel="Document"
          left={
            <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
              <HeaderFieldEditor
                fields={headerFields}
                reasons={data.task.reasons}
                drafts={drafts}
                saveStates={saveStates}
                activeFieldKey={activeFieldKey}
                onFieldFocus={focusField}
                onDraftChange={(fieldKey, value) =>
                  setDrafts((prev) => ({ ...prev, [fieldKey]: value }))
                }
                onSave={(fieldKey, value) => void enqueueSave(fieldKey, null, value)}
                readOnly={!editable}
              />

              {editable && activeFieldKey !== null && activeFieldKey in CATALOG_FIELD_LABELS ? (
                <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
                  <CatalogCandidatePicker
                    key={stateKey(activeFieldKey, activeRowIndex)}
                    fieldLabel={CATALOG_FIELD_LABELS[activeFieldKey] ?? activeFieldKey}
                    initialQuery={
                      (activeRowIndex === null ? drafts[activeFieldKey] : undefined) ??
                      serverValueFor(activeFieldKey, activeRowIndex).value ??
                      ""
                    }
                    loadCandidates={async (query) => {
                      catalogQueryRef.current = query;
                      const response = await fetchCatalogCandidates(
                        slug,
                        taskId,
                        activeFieldKey,
                        query,
                      );
                      if (!response.available) {
                        throw new Error(response.reason ?? "No catalog is bound to this stream.");
                      }
                      return response.candidates;
                    }}
                    onPick={(candidate, reason) => enqueuePick(candidate, reason)}
                  />
                  {catalogMessage ? (
                    <p role="status" style={{ margin: 0, font: "var(--soa-font-caption)" }}>
                      {catalogMessage}
                    </p>
                  ) : null}
                </div>
              ) : null}

              {Object.keys({
                ...data.line_items,
                ...(pendingRows.length ? { lines: [] } : {}),
              }).map((table) => {
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
                    onCellFocus={(fieldKey, rowIndex) => focusField(fieldKey, rowIndex)}
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
                      const position = rows.findIndex(
                        (candidate) => candidate.rowIndex === rowIndex,
                      );
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
              })}
            </div>
          }
          right={
            <DocumentViewer
              organizationSlug={slug}
              documentId={data.document.id}
              evidence={evidence}
              activeEvidenceId={activeEvidenceId}
              onEvidenceSelect={(id) => focusField(id.split("#")[0] ?? "")}
              drawTarget={editable && activeFieldKey !== null ? activeFieldKey : null}
              onRegionDrawn={onRegionDrawn}
            />
          }
        />

        <ReviewComments organizationSlug={slug} taskId={taskId} />

        <ApprovalPanel
          editable={editable}
          readOnlyReason={readOnlyReason}
          blocking={revalidatedBlocking ?? data.task.blocking}
          decision={currentDecision}
          canApprove={session.permissions.has("documents.approve")}
          canOverride={session.permissions.has("documents.approve.override")}
          canReject={session.permissions.has("documents.reject")}
          destination={
            (data.context["stream_name"] as string | undefined) ??
            (data.context["stream_slug"] as string | undefined) ??
            null
          }
          settledOutcome={data.task.state === "completed" ? data.task.outcome : null}
          busy={approve.isPending || reject.isPending || escalate.isPending}
          approvalBlockedReason={approvalBlockedReason}
          statusMessage={completionMessage}
          onApprove={(overrideReason) => {
            // Defense in depth for non-pointer activation and future panel
            // refactors: the disabled UI is not the only approval guard.
            if (approvalBlockedReason !== null) {
              setCompletionMessage(`Approval blocked: ${approvalBlockedReason}`);
              return;
            }
            approve.mutate(overrideReason);
          }}
          onReject={(reason) => reject.mutate(reason)}
          onEscalate={(reason) => escalate.mutate(reason)}
        />
      </div>
    </AppShell>
  );
}
