/**
 * Document detail (ING-012, UI_UX_BLUEPRINT §5.4).
 *
 * A stable deep link per document: summary and state (with the safe
 * reason for exceptional states), files with authorized short-lived
 * downloads (blocked server-side for quarantined documents), the
 * configuration context the stream currently pins, and the full audit
 * timeline. The processing panel (PRC-014) shows every run with its
 * stage attempts — provider, latency, warnings, safe errors — refreshing
 * itself in place while the pipeline moves (scroll and dialogs survive),
 * and offers retry/reprocess with the consequence spelled out before and
 * after. Canonical data and delivery history come from their live APIs;
 * corrections intentionally hand off to Review Studio. Actions are
 * state-specific: cancel appears only while the state machine allows it.
 */

import {
  Badge,
  Banner,
  Button,
  Dialog,
  DialogTrigger,
  Skeleton,
  TextField,
} from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";

import {
  approveDocumentDeletion,
  cancelDocumentDeletionRequest,
  cancelDocument,
  fetchCanonicalPayload,
  fetchDocumentDeletionRequest,
  fetchDocumentDetail,
  fetchDocumentLegalHolds,
  fetchDocumentRuns,
  placeDocumentLegalHold,
  reprocessDocument,
  releaseDocumentLegalHold,
  requestArtifactDownload,
  requestDocumentDeletion,
  type DocumentArtifact,
  type DocumentDeletionRequest,
  type DocumentLegalHold,
  type ProcessingRunEntry,
  type StageRunEntry,
} from "../api/client";
import { PayloadViewer } from "../components/canonical/PayloadViewer";
import { DeliveryHistory } from "../components/exports/DeliveryHistory";
import { DocumentViewer } from "../components/viewer/DocumentViewer";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const CANCELLABLE_STATES = new Set([
  "received",
  "validating_file",
  "queued",
  "preprocessing",
  "classifying",
  "splitting",
  "extracting",
  "normalizing",
  "validating_data",
  "review_required",
  "exporting",
]);

//: While the pipeline is moving, the runs panel refreshes itself; the
//: page never remounts, so scroll position and open dialogs survive.
const LIVE_STATES = new Set([
  "received",
  "validating_file",
  "queued",
  "preprocessing",
  "classifying",
  "splitting",
  "extracting",
  "normalizing",
  "validating_data",
  "exporting",
]);

const REPROCESSABLE_STATES = new Set(["review_required", "failed_retryable", "failed_terminal"]);

//: The approval-gated deletion lifecycle accepts settled terminal records.
//: A request alone never erases data; another principal must approve it.
const DELETION_REQUEST_STATES = new Set([
  "completed",
  "rejected",
  "quarantined",
  "failed_terminal",
  "cancelled",
  "archived",
]);

const DELETION_TERMINAL_STATES = new Set<DocumentDeletionRequest["state"]>([
  "failed",
  "completed",
  "cancelled",
]);
const DELETION_CANCELLABLE_STATES = new Set<DocumentDeletionRequest["state"]>([
  "pending_approval",
  "approved",
  "failed",
]);
const DELETION_POLL_MS = 4000;

const DELETION_STATE_TONES: Record<
  DocumentDeletionRequest["state"],
  "neutral" | "accent" | "success" | "warning" | "critical" | "info"
> = {
  pending_approval: "warning",
  approved: "accent",
  running: "info",
  failed: "critical",
  completed: "success",
  cancelled: "neutral",
};

//: The canonical payload exists once a document was approved (CAN-003).
const CANONICAL_STATES = new Set(["approved", "exporting", "completed", "archived"]);

const STATE_TONES: Record<
  string,
  "neutral" | "accent" | "success" | "warning" | "critical" | "info"
> = {
  queued: "info",
  review_required: "warning",
  approved: "success",
  completed: "success",
  rejected: "critical",
  quarantined: "critical",
  failed_retryable: "warning",
  failed_terminal: "critical",
  cancelled: "neutral",
  deleted: "neutral",
};

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section
      aria-label={title}
      style={{
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
        background: "var(--soa-surface)",
        padding: "var(--soa-space-5)",
      }}
    >
      <h2 style={{ margin: "0 0 var(--soa-space-3)", font: "var(--soa-font-heading-md)" }}>
        {title}
      </h2>
      {children}
    </section>
  );
}

function CancelDialog({ onConfirm }: { onConfirm: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  return (
    <Dialog title="Cancel this document?" alert>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0 }}>
            Processing stops and the document settles as cancelled. The reason is recorded in the
            audit log.
          </p>
          <TextField label="Reason (required)" value={reason} onChange={setReason} isRequired />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Keep processing
            </Button>
            <Button
              variant="destructive"
              isDisabled={reason.trim().length < 3}
              onPress={() => {
                onConfirm(reason.trim());
                close();
              }}
            >
              Cancel document
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function DeletionRequestDialog({ onConfirm }: { onConfirm: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  return (
    <Dialog title="Request deletion of this document’s data?" alert>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0 }}>
            This records an erasure request. A different authorized principal must review and
            approve it before the durable worker can permanently delete any data.
          </p>
          <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
            An active legal hold blocks approval. If approved and completed, a counts-only audit
            tombstone remains and the document stays listed as deleted.
          </p>
          <TextField label="Reason (required)" value={reason} onChange={setReason} isRequired />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Cancel request
            </Button>
            <Button
              variant="destructive"
              isDisabled={reason.trim().length < 3}
              onPress={() => {
                onConfirm(reason.trim());
                close();
              }}
            >
              Request deletion
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function DeletionDecisionDialog({
  decision,
  onConfirm,
}: {
  decision: "approve" | "cancel";
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  const approving = decision === "approve";
  return (
    <Dialog
      title={approving ? "Approve permanent document deletion?" : "Cancel deletion request?"}
      alert
    >
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0 }}>
            {approving
              ? "Approval queues durable erasure of the original file, derived data, review history, and outputs. This cannot be undone after execution starts."
              : "Cancellation stops a pending or approved request before execution starts. The cancellation and its reason remain audited."}
          </p>
          {approving ? (
            <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
              Two-person approval is enforced by the server: the requester cannot approve their own
              request, and an active legal hold blocks approval.
            </p>
          ) : null}
          <TextField label="Reason (required)" value={reason} onChange={setReason} isRequired />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Keep request
            </Button>
            {approving ? (
              <Button
                variant="destructive"
                isDisabled={reason.trim().length < 3}
                onPress={() => {
                  onConfirm(reason.trim());
                  close();
                }}
              >
                Approve deletion
              </Button>
            ) : (
              <Button
                isDisabled={reason.trim().length < 3}
                onPress={() => {
                  onConfirm(reason.trim());
                  close();
                }}
              >
                Cancel deletion request
              </Button>
            )}
          </div>
        </div>
      )}
    </Dialog>
  );
}

function LegalHoldDialog({
  action,
  onConfirm,
}: {
  action: "place" | "release";
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  const placing = action === "place";
  return (
    <Dialog title={placing ? "Place legal hold?" : "Release legal hold?"} alert>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0 }}>
            {placing
              ? "An active legal hold blocks document erasure and clears any unexecuted deletion approval."
              : "Releasing the hold ends this preservation block. It does not resume deletion automatically; independent approval is required again."}
          </p>
          <TextField label="Reason (required)" value={reason} onChange={setReason} isRequired />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Keep current hold state
            </Button>
            <Button
              variant={placing ? undefined : "destructive"}
              isDisabled={reason.trim().length < 3}
              onPress={() => {
                onConfirm(reason.trim());
                close();
              }}
            >
              {placing ? "Place legal hold" : "Release legal hold"}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function sameVisiblePrincipal(requestedBy: string, userId: string): boolean {
  return requestedBy === userId || requestedBy === `user:${userId}`;
}

function containsDocumentReference(
  value: unknown,
  documentId: string,
  seen = new WeakSet<object>(),
): boolean {
  if (value === documentId) return true;
  if (value === null || typeof value !== "object") return false;
  if (seen.has(value)) return false;
  seen.add(value);
  return Object.values(value).some((child) => containsDocumentReference(child, documentId, seen));
}

function ReprocessDialog({
  mode,
  explanation,
  onConfirm,
}: {
  mode: "retry" | "current_config";
  explanation: string;
  onConfirm: (mode: "retry" | "current_config", reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <Dialog title={mode === "retry" ? "Retry this document?" : "Reprocess this document?"}>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0 }}>{explanation}</p>
          <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
            Previous runs and their artifacts remain unchanged as evidence.
          </p>
          <TextField label="Reason (required)" value={reason} onChange={setReason} isRequired />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Keep as is
            </Button>
            <Button
              isDisabled={reason.trim().length < 3}
              onPress={() => {
                onConfirm(mode, reason.trim());
                close();
              }}
            >
              {mode === "retry" ? "Retry" : "Reprocess"}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function StageRow({ stage }: { stage: StageRunEntry }) {
  const warnings = Array.isArray(stage.output_summary["warnings"])
    ? (stage.output_summary["warnings"] as unknown[]).map(String)
    : [];
  return (
    <li
      style={{
        display: "flex",
        gap: "var(--soa-space-3)",
        alignItems: "baseline",
        flexWrap: "wrap",
        padding: "var(--soa-space-2) 0",
        borderBottom: "1px solid var(--soa-border)",
      }}
    >
      <strong style={{ minWidth: "9rem" }}>{stage.stage.replace(/_/g, " ")}</strong>
      <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
        attempt {stage.attempt}
      </span>
      <Badge
        tone={
          stage.state === "succeeded" ? "success" : stage.state === "failed" ? "critical" : "info"
        }
      >
        {stage.state}
      </Badge>
      {stage.provider ? <Badge tone="neutral">{stage.provider}</Badge> : null}
      {stage.latency_ms !== null ? (
        <span style={{ font: "var(--soa-font-caption)" }}>{stage.latency_ms} ms</span>
      ) : null}
      {stage.safe_error ? (
        <span style={{ color: "var(--soa-text-muted)" }}>
          {stage.safe_error}
          {stage.failure_class ? ` (${stage.failure_class})` : null}
        </span>
      ) : null}
      {warnings.map((warning) => (
        <Badge key={warning} tone="warning">
          {warning}
        </Badge>
      ))}
    </li>
  );
}

function RunBlock({ run }: { run: ProcessingRunEntry }) {
  return (
    <section aria-label={`Run ${run.run_number}`} style={{ display: "grid", gap: "0.25rem" }}>
      <div
        style={{
          display: "flex",
          gap: "var(--soa-space-3)",
          alignItems: "baseline",
          flexWrap: "wrap",
        }}
      >
        <h3 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>Run {run.run_number}</h3>
        <Badge
          tone={
            run.state === "succeeded" ? "success" : run.state === "failed" ? "critical" : "info"
          }
        >
          {run.state}
        </Badge>
        <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
          {run.triggered_by} · {run.total_latency_ms} ms
          {run.config_fingerprint ? (
            <>
              {" "}
              · config <code>{run.config_fingerprint.slice(0, 12)}…</code>
            </>
          ) : null}
          {run.contract_fingerprint ? (
            <>
              {" "}
              · contract <code>{run.contract_fingerprint.slice(0, 12)}…</code>
            </>
          ) : null}
          {run.runtime_fingerprint ? (
            <>
              {" "}
              · runtime <code>{run.runtime_fingerprint.slice(0, 12)}…</code>
            </>
          ) : null}
        </span>
      </div>
      <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {run.stages.map((stage) => (
          <StageRow key={`${stage.stage}:${stage.attempt}`} stage={stage} />
        ))}
      </ol>
    </section>
  );
}

function ArtifactRow({
  artifact,
  onDownload,
}: {
  artifact: DocumentArtifact;
  onDownload: () => void;
}) {
  return (
    <li
      style={{
        display: "flex",
        gap: "var(--soa-space-3)",
        alignItems: "center",
        flexWrap: "wrap",
        padding: "var(--soa-space-2) 0",
        borderBottom: "1px solid var(--soa-border)",
      }}
    >
      <Badge tone="neutral">{artifact.kind}</Badge>
      <code style={{ font: "var(--soa-font-caption)" }}>{artifact.sha256.slice(0, 12)}…</code>
      <span style={{ font: "var(--soa-font-caption)" }}>
        {Math.max(1, Math.round(artifact.size_bytes / 1024))} KB · {artifact.content_type}
      </span>
      <Button
        size="sm"
        variant="subtle"
        aria-label={`Download ${artifact.kind}`}
        onPress={onDownload}
      >
        Download
      </Button>
    </li>
  );
}

export function DocumentDetail() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canReview = session.permissions.has("documents.review");
  const canReadDeletionStatus =
    session.permissions.has("data.delete.request") ||
    session.permissions.has("data.delete.approve");
  const canManageRetention = session.permissions.has("data.retention.manage");
  const { documentId } = useParams({ strict: false }) as { documentId: string };
  const queryClient = useQueryClient();
  const detailQueryKey = useMemo(() => ["document", slug, documentId] as const, [slug, documentId]);
  const deletionQueryKey = useMemo(
    () => ["document-deletion-request", slug, documentId] as const,
    [slug, documentId],
  );
  const legalHoldsQueryKey = useMemo(
    () => ["document-legal-holds", slug, documentId] as const,
    [slug, documentId],
  );

  const detail = useQuery({
    queryKey: detailQueryKey,
    queryFn: () => fetchDocumentDetail(slug, documentId),
    // Live refresh while the pipeline is moving, so the summary state and
    // timeline track the runs panel below instead of freezing at load time.
    // A durable deletion request also drives this poll until it settles; a
    // completed request gets one final convergence loop until the API returns
    // the deleted tombstone rather than stale pre-erasure detail.
    refetchInterval: (query) => {
      const request = queryClient.getQueryData<DocumentDeletionRequest | null>(deletionQueryKey);
      const deletionInFlight =
        request !== undefined && request !== null
          ? !DELETION_TERMINAL_STATES.has(request.state)
          : false;
      const awaitingTombstone =
        request?.state === "completed" && query.state.data?.document.state !== "deleted";
      return (query.state.data && LIVE_STATES.has(query.state.data.document.state)) ||
        deletionInFlight ||
        awaitingTombstone
        ? DELETION_POLL_MS
        : false;
    },
    refetchOnWindowFocus: true,
  });
  const deletionStatus = useQuery({
    queryKey: deletionQueryKey,
    queryFn: () => fetchDocumentDeletionRequest(slug, documentId),
    enabled:
      canReadDeletionStatus &&
      detail.data !== undefined &&
      detail.data.document.state !== "deleted",
    refetchInterval: (query) => {
      const request = query.state.data;
      return request && !DELETION_TERMINAL_STATES.has(request.state) ? DELETION_POLL_MS : false;
    },
    refetchOnWindowFocus: true,
    retry: false,
  });
  const legalHolds = useQuery({
    queryKey: legalHoldsQueryKey,
    queryFn: () => fetchDocumentLegalHolds(slug, documentId),
    enabled:
      canManageRetention && detail.data !== undefined && detail.data.document.state !== "deleted",
    refetchOnWindowFocus: true,
    retry: false,
  });
  const runs = useQuery({
    queryKey: ["document-runs", slug, documentId],
    queryFn: () => fetchDocumentRuns(slug, documentId),
    enabled: detail.data !== undefined && detail.data.document.state !== "deleted",
    // Live refresh while the pipeline is moving: data refreshes in place
    // (no remount), so scroll position and open dialogs are preserved.
    refetchInterval: (query) =>
      query.state.data && LIVE_STATES.has(query.state.data.state) ? 4000 : false,
    refetchOnWindowFocus: true,
  });

  const canonical = useQuery({
    queryKey: ["canonical-payload", slug, documentId],
    queryFn: () => fetchCanonicalPayload(slug, documentId),
    enabled: detail.data !== undefined && CANONICAL_STATES.has(detail.data.document.state),
    retry: false,
  });

  const cancel = useMutation({
    mutationKey: ["cancel-document", slug, documentId],
    mutationFn: (reason: string) => cancelDocument(slug, documentId, reason),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["document", slug, documentId] });
      void queryClient.invalidateQueries({ queryKey: ["document-runs", slug, documentId] });
    },
  });
  const deletionRequest = useMutation({
    mutationKey: ["request-document-deletion", slug, documentId],
    mutationFn: (reason: string) => requestDocumentDeletion(slug, documentId, reason),
    onSuccess: (request) => {
      queryClient.setQueryData(deletionQueryKey, request);
      void queryClient.invalidateQueries({ queryKey: ["documents", slug] });
      void queryClient.invalidateQueries({ queryKey: detailQueryKey });
      void queryClient.invalidateQueries({ queryKey: ["document-runs", slug, documentId] });
    },
  });
  const approveDeletion = useMutation({
    mutationKey: ["approve-document-deletion", slug, documentId],
    mutationFn: ({ requestId, reason }: { requestId: string; reason: string }) =>
      approveDocumentDeletion(slug, requestId, reason),
    onSuccess: (request) => {
      queryClient.setQueryData(deletionQueryKey, request);
      void queryClient.invalidateQueries({ queryKey: ["documents", slug] });
      void queryClient.invalidateQueries({ queryKey: detailQueryKey });
    },
  });
  const cancelDeletion = useMutation({
    mutationKey: ["cancel-document-deletion", slug, documentId],
    mutationFn: ({ requestId, reason }: { requestId: string; reason: string }) =>
      cancelDocumentDeletionRequest(slug, requestId, reason),
    onSuccess: (request) => {
      queryClient.setQueryData(deletionQueryKey, request);
      void queryClient.invalidateQueries({ queryKey: ["documents", slug] });
      void queryClient.invalidateQueries({ queryKey: detailQueryKey });
    },
  });
  const placeLegalHold = useMutation({
    mutationKey: ["place-document-legal-hold", slug, documentId],
    mutationFn: (reason: string) => placeDocumentLegalHold(slug, documentId, reason),
    onSuccess: (hold) => {
      queryClient.setQueryData<{ items: DocumentLegalHold[] }>(legalHoldsQueryKey, (current) => ({
        items: [hold, ...(current?.items.filter((item) => item.id !== hold.id) ?? [])],
      }));
      void queryClient.invalidateQueries({ queryKey: deletionQueryKey });
      void queryClient.invalidateQueries({ queryKey: detailQueryKey });
    },
  });
  const releaseLegalHold = useMutation({
    mutationKey: ["release-document-legal-hold", slug, documentId],
    mutationFn: ({ holdId, reason }: { holdId: string; reason: string }) =>
      releaseDocumentLegalHold(slug, holdId, reason),
    onSuccess: (hold) => {
      queryClient.setQueryData<{ items: DocumentLegalHold[] }>(legalHoldsQueryKey, (current) => ({
        items: current?.items.map((item) => (item.id === hold.id ? hold : item)) ?? [hold],
      }));
      void queryClient.invalidateQueries({ queryKey: deletionQueryKey });
    },
  });
  const reprocess = useMutation({
    mutationKey: ["reprocess-document", slug, documentId],
    mutationFn: (options: { mode: "retry" | "current_config"; reason: string }) =>
      reprocessDocument(slug, documentId, options),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["document", slug, documentId] });
      void queryClient.invalidateQueries({ queryKey: ["document-runs", slug, documentId] });
    },
  });
  const download = useMutation({
    mutationKey: ["download-document-artifact", slug, documentId],
    mutationFn: (artifactId: string) => requestArtifactDownload(slug, artifactId),
    onSuccess: (signed) => {
      window.open(signed.url, "_blank", "noopener");
    },
  });

  const deletedDataUpdatedAt = detail.data?.document.state === "deleted" ? detail.dataUpdatedAt : 0;
  useEffect(() => {
    if (deletedDataUpdatedAt === 0) return;

    // Keep only the already-sanitized tombstone query. Evict exact keyed
    // children (runs/pages/canonical/deliveries) and any broader list or
    // workspace whose cached JSON still references this document.
    queryClient.removeQueries({
      predicate: (query) => {
        const key = query.queryKey;
        const isCurrentTombstone =
          key.length === detailQueryKey.length &&
          key.every((part, index) => part === detailQueryKey[index]);
        if (isCurrentTombstone) return false;
        return key.includes(documentId) || containsDocumentReference(query.state.data, documentId);
      },
    });

    // Signed download URLs and action responses live in the mutation cache,
    // not the query cache. Mutation keys make them attributable and purgeable.
    const mutationCache = queryClient.getMutationCache();
    for (const mutation of mutationCache.getAll()) {
      if (mutation.options.mutationKey?.includes(documentId)) mutationCache.remove(mutation);
    }
  }, [deletedDataUpdatedAt, detailQueryKey, documentId, queryClient]);

  if (detail.status === "pending") {
    return (
      <AppShell title="Document" breadcrumbs={[{ label: session.organization.name }]}>
        <Skeleton height="12rem" />
      </AppShell>
    );
  }
  if (detail.status === "error") {
    return (
      <AppShell title="Document" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load this document"
          action={
            <Button size="sm" onPress={() => void detail.refetch()}>
              Try again
            </Button>
          }
        >
          It may have been removed, or the service did not respond.
        </Banner>
      </AppShell>
    );
  }

  const { document, artifacts, context, timeline } = detail.data;
  const isDeleted = document.state === "deleted";
  if (isDeleted) {
    const tombstone = detail.data.deletion_tombstone;
    const recordsCleared = Object.values(tombstone?.category_counts ?? {}).reduce(
      (total, count) => total + count,
      0,
    );
    return (
      <AppShell
        title="Deleted document"
        breadcrumbs={[
          { label: session.organization.name },
          { label: "Documents", to: "/app/$organizationSlug/documents" },
          { label: "Deleted document" },
        ]}
      >
        <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "56rem" }}>
          <Banner tone="neutral" title="This document’s data has been deleted">
            The file and all document-scoped derived data are permanently unavailable. Only this
            counts-only deletion tombstone remains in the browser.
          </Banner>
          <Panel title="Deletion tombstone">
            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "12rem minmax(0, 1fr)",
                gap: "0.5rem",
                margin: 0,
              }}
            >
              <dt>Document ID</dt>
              <dd style={{ margin: 0 }}>
                <code>{document.id}</code>
              </dd>
              <dt>State</dt>
              <dd style={{ margin: 0 }}>
                <Badge tone="neutral">deleted</Badge>
              </dd>
              <dt>Completed</dt>
              <dd style={{ margin: 0 }}>
                {tombstone?.completed_at
                  ? new Date(tombstone.completed_at).toLocaleString()
                  : "Retained in the deletion ledger"}
              </dd>
              {tombstone?.object_keys_deleted !== null &&
              tombstone?.object_keys_deleted !== undefined ? (
                <>
                  <dt>Stored objects erased</dt>
                  <dd style={{ margin: 0 }}>{tombstone.object_keys_deleted}</dd>
                </>
              ) : null}
              {recordsCleared > 0 ? (
                <>
                  <dt>Scoped records cleared</dt>
                  <dd style={{ margin: 0 }}>{recordsCleared}</dd>
                </>
              ) : null}
            </dl>
          </Panel>
        </div>
      </AppShell>
    );
  }

  const durableDeletion = deletionStatus.data ?? null;
  const activeLegalHold = legalHolds.data?.items.find((hold) => hold.state === "active") ?? null;
  const legalHoldStatusUnverified = canManageRetention && legalHolds.status !== "success";
  const cancellable = canReview && CANCELLABLE_STATES.has(document.state);
  const canReprocess =
    session.permissions.has("documents.reprocess") && REPROCESSABLE_STATES.has(document.state);
  const canRequestDeletion =
    session.permissions.has("data.delete.request") &&
    DELETION_REQUEST_STATES.has(document.state) &&
    deletionStatus.status === "success" &&
    (durableDeletion === null || durableDeletion.state === "cancelled");
  const selfRequestedDeletion =
    durableDeletion !== null && sameVisiblePrincipal(durableDeletion.requested_by, session.userId);
  const canApproveDeletion =
    durableDeletion?.state === "pending_approval" && session.permissions.has("data.delete.approve");
  const canCancelDeletion =
    durableDeletion !== null &&
    session.permissions.has("data.delete.request") &&
    DELETION_CANCELLABLE_STATES.has(durableDeletion.state);
  const hasRuns = (runs.data?.runs.length ?? 0) > 0;

  return (
    <AppShell
      title={document.original_filename}
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Documents", to: "/app/$organizationSlug/documents" },
        { label: document.original_filename },
      ]}
      actions={
        cancellable || canRequestDeletion ? (
          <span style={{ display: "inline-flex", gap: "var(--soa-space-2)" }}>
            {cancellable ? (
              <DialogTrigger>
                <Button variant="destructive">Cancel document</Button>
                <CancelDialog onConfirm={(reason) => cancel.mutate(reason)} />
              </DialogTrigger>
            ) : null}
            {canRequestDeletion ? (
              <DialogTrigger>
                <Button variant="destructive">Request deletion</Button>
                <DeletionRequestDialog onConfirm={(reason) => deletionRequest.mutate(reason)} />
              </DialogTrigger>
            ) : null}
          </span>
        ) : undefined
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "56rem" }}>
        {cancel.isError ? (
          <Banner tone="critical" title="Cancel failed">
            {cancel.error?.message ?? "The document was not changed."}
          </Banner>
        ) : null}
        {deletionRequest.isError ? (
          <Banner tone="critical" title="Deletion request failed">
            {deletionRequest.error?.message ?? "No deletion request was created."}
          </Banner>
        ) : null}
        {deletionRequest.isSuccess ? (
          <Banner tone="success" title="Deletion request submitted">
            No data has been deleted. A different authorized principal must approve the request
            before the durable deletion worker can run.
          </Banner>
        ) : null}
        {download.isError ? (
          <Banner tone="critical" title="Download refused">
            {download.error?.message ?? "No file was downloaded."}
          </Banner>
        ) : null}

        {canManageRetention ? (
          <Panel title="Legal hold">
            {legalHolds.status === "pending" ? (
              <Skeleton height="4rem" />
            ) : legalHolds.status === "error" ? (
              <Banner
                tone="critical"
                title="Couldn’t load legal holds"
                action={
                  <Button size="sm" onPress={() => void legalHolds.refetch()}>
                    Try again
                  </Button>
                }
              >
                Preservation controls stay unavailable until the legal-hold ledger can be read.
              </Banner>
            ) : activeLegalHold ? (
              <div style={{ display: "grid", gap: "var(--soa-space-3)" }}>
                <Banner tone="warning" title="Active legal hold">
                  Deletion approval and execution are blocked while this hold remains active.
                </Banner>
                <dl
                  style={{
                    display: "grid",
                    gridTemplateColumns: "12rem minmax(0, 1fr)",
                    gap: "0.5rem",
                    margin: 0,
                  }}
                >
                  <dt>Reason</dt>
                  <dd style={{ margin: 0 }}>{activeLegalHold.reason}</dd>
                  <dt>Placed by</dt>
                  <dd style={{ margin: 0 }}>{activeLegalHold.placed_by}</dd>
                  <dt>Placed</dt>
                  <dd style={{ margin: 0 }}>
                    {new Date(activeLegalHold.placed_at).toLocaleString()}
                  </dd>
                </dl>
                {releaseLegalHold.isError ? (
                  <Banner tone="critical" title="Hold release refused">
                    {releaseLegalHold.error?.message ?? "The legal hold remains active."}
                  </Banner>
                ) : null}
                <div>
                  <DialogTrigger>
                    <Button variant="destructive">Release legal hold</Button>
                    <LegalHoldDialog
                      action="release"
                      onConfirm={(reason) =>
                        releaseLegalHold.mutate({ holdId: activeLegalHold.id, reason })
                      }
                    />
                  </DialogTrigger>
                </div>
              </div>
            ) : (
              <div style={{ display: "grid", gap: "var(--soa-space-3)" }}>
                <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
                  No active legal hold. Previously released holds remain in the server ledger.
                </p>
                {placeLegalHold.isError ? (
                  <Banner tone="critical" title="Couldn’t place legal hold">
                    {placeLegalHold.error?.message ?? "No legal hold was created."}
                  </Banner>
                ) : null}
                <div>
                  <DialogTrigger>
                    <Button>Place legal hold</Button>
                    <LegalHoldDialog
                      action="place"
                      onConfirm={(reason) => placeLegalHold.mutate(reason)}
                    />
                  </DialogTrigger>
                </div>
              </div>
            )}
          </Panel>
        ) : null}

        {canReadDeletionStatus ? (
          <Panel title="Deletion request">
            {deletionStatus.status === "pending" ? (
              <Skeleton height="4rem" />
            ) : deletionStatus.status === "error" ? (
              <Banner
                tone="critical"
                title="Couldn’t load deletion status"
                action={
                  <Button size="sm" onPress={() => void deletionStatus.refetch()}>
                    Try again
                  </Button>
                }
              >
                Deletion controls stay unavailable until the durable request ledger can be read.
              </Banner>
            ) : durableDeletion === null ? (
              <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
                No deletion request has been recorded for this document.
              </p>
            ) : (
              <div style={{ display: "grid", gap: "var(--soa-space-3)" }}>
                <dl
                  style={{
                    display: "grid",
                    gridTemplateColumns: "12rem minmax(0, 1fr)",
                    gap: "0.5rem",
                    margin: 0,
                  }}
                >
                  <dt>State</dt>
                  <dd style={{ margin: 0 }}>
                    <Badge tone={DELETION_STATE_TONES[durableDeletion.state]}>
                      {durableDeletion.state.replace(/_/g, " ")}
                    </Badge>
                  </dd>
                  <dt>Reason</dt>
                  <dd style={{ margin: 0 }}>{durableDeletion.reason}</dd>
                  <dt>Requested by</dt>
                  <dd style={{ margin: 0 }}>{durableDeletion.requested_by}</dd>
                  <dt>Requested</dt>
                  <dd style={{ margin: 0 }}>
                    {new Date(durableDeletion.requested_at).toLocaleString()}
                  </dd>
                  {durableDeletion.safe_error ? (
                    <>
                      <dt>Safe error</dt>
                      <dd style={{ margin: 0 }}>{durableDeletion.safe_error}</dd>
                    </>
                  ) : null}
                </dl>

                {durableDeletion.state === "pending_approval" ? (
                  <Banner
                    tone="warning"
                    title={
                      activeLegalHold
                        ? "Legal hold blocks deletion"
                        : "Independent approval required"
                    }
                  >
                    {activeLegalHold
                      ? "An active preservation hold prevents approval and erasure. Releasing it does not resume deletion automatically."
                      : selfRequestedDeletion
                        ? "You submitted this request, so you cannot approve it. Ask a different authorized principal."
                        : "The server enforces two-person approval: the principal who submitted this request cannot approve it."}
                  </Banner>
                ) : durableDeletion.state === "approved" ? (
                  <Banner tone="info" title="Deletion approved">
                    The durable worker is queued. This page will keep refreshing until erasure
                    starts and the request reaches a terminal state.
                  </Banner>
                ) : durableDeletion.state === "running" ? (
                  <Banner tone="info" title="Deletion in progress">
                    Document detail is refreshing until the counts-only tombstone is available.
                  </Banner>
                ) : durableDeletion.state === "failed" ? (
                  <Banner tone="critical" title="Deletion attempt failed">
                    The durable job may retry. An authorized requester can cancel the lifecycle if
                    execution is not running.
                  </Banner>
                ) : durableDeletion.state === "cancelled" ? (
                  <Banner tone="neutral" title="Deletion request cancelled">
                    No deletion is in progress. A corrected request can be submitted if needed.
                  </Banner>
                ) : null}

                {approveDeletion.isError ? (
                  <Banner tone="critical" title="Approval refused">
                    {approveDeletion.error?.message ?? "The deletion request was not approved."}
                  </Banner>
                ) : null}
                {cancelDeletion.isError ? (
                  <Banner tone="critical" title="Cancellation refused">
                    {cancelDeletion.error?.message ?? "The deletion request was not cancelled."}
                  </Banner>
                ) : null}

                {canApproveDeletion || canCancelDeletion ? (
                  <div style={{ display: "flex", gap: "var(--soa-space-2)", flexWrap: "wrap" }}>
                    {canApproveDeletion ? (
                      selfRequestedDeletion ||
                      activeLegalHold !== null ||
                      legalHoldStatusUnverified ? (
                        <Button variant="destructive" isDisabled>
                          Approve deletion
                        </Button>
                      ) : (
                        <DialogTrigger>
                          <Button variant="destructive">Approve deletion</Button>
                          <DeletionDecisionDialog
                            decision="approve"
                            onConfirm={(reason) =>
                              approveDeletion.mutate({ requestId: durableDeletion.id, reason })
                            }
                          />
                        </DialogTrigger>
                      )
                    ) : null}
                    {canCancelDeletion ? (
                      <DialogTrigger>
                        <Button variant="subtle">Cancel deletion request</Button>
                        <DeletionDecisionDialog
                          decision="cancel"
                          onConfirm={(reason) =>
                            cancelDeletion.mutate({ requestId: durableDeletion.id, reason })
                          }
                        />
                      </DialogTrigger>
                    ) : null}
                  </div>
                ) : null}
              </div>
            )}
          </Panel>
        ) : null}

        <Panel title="Summary">
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "12rem minmax(0, 1fr)",
              gap: "0.5rem",
              margin: 0,
            }}
          >
            <dt>State</dt>
            <dd style={{ margin: 0, display: "flex", gap: "var(--soa-space-1)" }}>
              <Badge tone={STATE_TONES[document.state] ?? "neutral"}>
                {document.state.replace(/_/g, " ")}
              </Badge>
              {document.duplicate_of ? <Badge tone="warning">duplicate</Badge> : null}
            </dd>
            {document.state_reason ? (
              <>
                <dt>Reason</dt>
                <dd style={{ margin: 0 }}>{document.state_reason}</dd>
              </>
            ) : null}
            <dt>Stream</dt>
            <dd style={{ margin: 0 }}>
              {context.stream_slug ? (
                <Link
                  to="/app/$organizationSlug/streams/$streamSlug"
                  params={{ organizationSlug: slug, streamSlug: context.stream_slug }}
                >
                  {context.stream_name ?? context.stream_slug}
                </Link>
              ) : (
                "—"
              )}
              {context.stream_version_number !== undefined
                ? ` · configuration v${context.stream_version_number}`
                : null}
            </dd>
            <dt>Channel</dt>
            <dd style={{ margin: 0 }}>{document.source_channel}</dd>
            <dt>Received</dt>
            <dd style={{ margin: 0 }}>{new Date(document.received_at).toLocaleString()}</dd>
            <dt>Client reference</dt>
            <dd style={{ margin: 0 }}>{document.client_reference ?? "—"}</dd>
            <dt>Content hash</dt>
            <dd style={{ margin: 0 }}>
              <code>{document.content_sha256.slice(0, 16)}…</code>
            </dd>
          </dl>
        </Panel>

        <Panel title="Files">
          {artifacts.length === 0 ? (
            <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>No stored files.</p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {artifacts.map((artifact) => (
                <ArtifactRow
                  key={artifact.id}
                  artifact={artifact}
                  onDownload={() => download.mutate(artifact.id)}
                />
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Preview">
          <DocumentViewer organizationSlug={slug} documentId={documentId} />
        </Panel>

        <Panel title="Processing">
          {reprocess.isError ? (
            <Banner tone="critical" title="Reprocess refused">
              {reprocess.error?.message ?? "The document was not changed."}
            </Banner>
          ) : null}
          {reprocess.isSuccess ? (
            <Banner tone="success" title={`Run ${reprocess.data.run_number} queued`}>
              {reprocess.data.consequence}
            </Banner>
          ) : null}
          {canReprocess ? (
            <div
              style={{
                display: "flex",
                gap: "var(--soa-space-2)",
                marginBottom: "var(--soa-space-3)",
              }}
            >
              <DialogTrigger>
                <Button size="sm" isDisabled={!hasRuns}>
                  Retry (same configuration)
                </Button>
                <ReprocessDialog
                  mode="retry"
                  explanation="A new run will re-execute the full pipeline under the SAME configuration as the last run."
                  onConfirm={(mode, reason) => reprocess.mutate({ mode, reason })}
                />
              </DialogTrigger>
              <DialogTrigger>
                <Button size="sm" variant="subtle">
                  Reprocess (current configuration)
                </Button>
                <ReprocessDialog
                  mode="current_config"
                  explanation="A new run will re-execute the full pipeline under the stream's currently published configuration."
                  onConfirm={(mode, reason) => reprocess.mutate({ mode, reason })}
                />
              </DialogTrigger>
            </div>
          ) : null}
          {runs.status === "pending" ? (
            <Skeleton height="4rem" />
          ) : runs.status === "error" ? (
            <Banner tone="critical" title="Couldn’t load processing runs">
              <Button size="sm" onPress={() => void runs.refetch()}>
                Try again
              </Button>
            </Banner>
          ) : runs.data.runs.length === 0 ? (
            <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
              No processing runs yet — the document has not entered the pipeline.
            </p>
          ) : (
            <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
              {runs.data.runs.map((run) => (
                <RunBlock key={run.id} run={run} />
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Extracted data">
          <Badge tone="neutral">
            reviewed and corrected in the Review Studio — open the task from the Review queue
          </Badge>
        </Panel>
        <Panel title="Canonical order">
          {canonical.data ? (
            <PayloadViewer data={canonical.data} />
          ) : CANONICAL_STATES.has(document.state) && canonical.status === "pending" ? (
            <Skeleton height="6rem" />
          ) : (
            <Badge tone="neutral">
              not available yet — the canonical order is created when the document is approved
            </Badge>
          )}
        </Panel>
        <Panel title="Delivery">
          {session.permissions.has("integrations.read") ? (
            <DeliveryHistory
              organizationSlug={slug}
              documentId={documentId}
              canReplay={session.permissions.has("integrations.replay")}
            />
          ) : (
            <Badge tone="neutral">viewing deliveries needs the integrations.read permission</Badge>
          )}
        </Panel>

        <Panel title="Timeline">
          <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {timeline.map((entry, index) => (
              <li
                key={index}
                style={{
                  display: "flex",
                  gap: "var(--soa-space-3)",
                  alignItems: "baseline",
                  flexWrap: "wrap",
                  padding: "var(--soa-space-2) 0",
                  borderBottom: "1px solid var(--soa-border)",
                }}
              >
                <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                  {new Date(entry.occurred_at).toLocaleString()}
                </span>
                <strong>{entry.action}</strong>
                {typeof entry.summary["from"] === "string" &&
                typeof entry.summary["to"] === "string" ? (
                  <span>
                    {String(entry.summary["from"]).replace(/_/g, " ")} →{" "}
                    {String(entry.summary["to"]).replace(/_/g, " ")}
                  </span>
                ) : null}
                {typeof entry.summary["reason"] === "string" && entry.summary["reason"] ? (
                  <span style={{ color: "var(--soa-text-muted)" }}>
                    {String(entry.summary["reason"])}
                  </span>
                ) : null}
                <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                  {entry.actor_id}
                </span>
              </li>
            ))}
          </ol>
        </Panel>
      </div>
    </AppShell>
  );
}
