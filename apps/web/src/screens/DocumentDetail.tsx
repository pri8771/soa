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
 * after. Extracted data, validation, and delivery panels are honest
 * placeholders until REV/EXP land — never fake data. Actions are
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
import { useState } from "react";

import {
  cancelDocument,
  deleteDocumentData,
  fetchCanonicalPayload,
  fetchDocumentDetail,
  fetchDocumentRuns,
  reprocessDocument,
  requestArtifactDownload,
  type DocumentArtifact,
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

//: States the deletion endpoint accepts (SEC-010): settled or waiting on
//: a human — never a document still moving through the pipeline.
const DELETABLE_STATES = new Set([
  "review_required",
  "approved",
  "completed",
  "rejected",
  "quarantined",
  "failed_retryable",
  "failed_terminal",
  "cancelled",
  "archived",
]);

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

function DeleteDialog({ onConfirm }: { onConfirm: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  return (
    <Dialog title="Delete this document’s data?" alert>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0 }}>
            The original file, rendered pages, extracted data, review history, and outputs are
            permanently erased. This cannot be undone.
          </p>
          <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
            An audit tombstone recording who deleted what, when, and why is kept, and the document
            stays listed as deleted.
          </p>
          <TextField label="Reason (required)" value={reason} onChange={setReason} isRequired />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Keep the data
            </Button>
            <Button
              variant="destructive"
              isDisabled={reason.trim().length < 3}
              onPress={() => {
                onConfirm(reason.trim());
                close();
              }}
            >
              Delete document
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
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
  const { documentId } = useParams({ strict: false }) as { documentId: string };
  const queryClient = useQueryClient();

  const detail = useQuery({
    queryKey: ["document", slug, documentId],
    queryFn: () => fetchDocumentDetail(slug, documentId),
    // Live refresh while the pipeline is moving, so the summary state and
    // timeline track the runs panel below instead of freezing at load time.
    refetchInterval: (query) =>
      query.state.data && LIVE_STATES.has(query.state.data.document.state) ? 4000 : false,
    refetchOnWindowFocus: true,
  });
  const runs = useQuery({
    queryKey: ["document-runs", slug, documentId],
    queryFn: () => fetchDocumentRuns(slug, documentId),
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
    mutationFn: (reason: string) => cancelDocument(slug, documentId, reason),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["document", slug, documentId] });
      void queryClient.invalidateQueries({ queryKey: ["document-runs", slug, documentId] });
    },
  });
  const deletion = useMutation({
    mutationFn: (reason: string) => deleteDocumentData(slug, documentId, reason),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["documents", slug] });
      void queryClient.invalidateQueries({ queryKey: ["document", slug, documentId] });
      void queryClient.invalidateQueries({ queryKey: ["document-runs", slug, documentId] });
    },
  });
  const reprocess = useMutation({
    mutationFn: (options: { mode: "retry" | "current_config"; reason: string }) =>
      reprocessDocument(slug, documentId, options),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["document", slug, documentId] });
      void queryClient.invalidateQueries({ queryKey: ["document-runs", slug, documentId] });
    },
  });
  const download = useMutation({
    mutationFn: (artifactId: string) => requestArtifactDownload(slug, artifactId),
    onSuccess: (signed) => {
      window.open(signed.url, "_blank", "noopener");
    },
  });

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
  const cancellable = canReview && CANCELLABLE_STATES.has(document.state);
  const canReprocess =
    session.permissions.has("documents.reprocess") && REPROCESSABLE_STATES.has(document.state);
  const deletable = session.permissions.has("data.delete") && DELETABLE_STATES.has(document.state);
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
        cancellable || deletable ? (
          <span style={{ display: "inline-flex", gap: "var(--soa-space-2)" }}>
            {cancellable ? (
              <DialogTrigger>
                <Button variant="destructive">Cancel document</Button>
                <CancelDialog onConfirm={(reason) => cancel.mutate(reason)} />
              </DialogTrigger>
            ) : null}
            {deletable ? (
              <DialogTrigger>
                <Button variant="destructive">Delete document</Button>
                <DeleteDialog onConfirm={(reason) => deletion.mutate(reason)} />
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
        {deletion.isError ? (
          <Banner tone="critical" title="Delete failed">
            {deletion.error?.message ?? "Nothing was deleted."}
          </Banner>
        ) : null}
        {isDeleted ? (
          <Banner tone="neutral" title="This document’s data has been deleted">
            The original file, pages, extracted data, review history, and outputs were permanently
            erased{document.state_reason ? `: ${document.state_reason}` : "."} An audit tombstone of
            the deletion is retained in the timeline below.
          </Banner>
        ) : null}
        {download.isError ? (
          <Banner tone="critical" title="Download refused">
            {download.error?.message ?? "No file was downloaded."}
          </Banner>
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
          {isDeleted ? (
            <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
              No preview — the document’s data has been deleted.
            </p>
          ) : (
            <DocumentViewer organizationSlug={slug} documentId={documentId} />
          )}
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
