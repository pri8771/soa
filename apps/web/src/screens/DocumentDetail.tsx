/**
 * Document detail (ING-012, UI_UX_BLUEPRINT §5.4).
 *
 * A stable deep link per document: summary and state (with the safe
 * reason for exceptional states), files with authorized short-lived
 * downloads (blocked server-side for quarantined documents), the
 * configuration context the stream currently pins, and the full audit
 * timeline. Extracted data, validation, and delivery panels are honest
 * placeholders until PRC/REV/EXP land — never fake data. Actions are
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
  fetchDocumentDetail,
  requestArtifactDownload,
  type DocumentArtifact,
} from "../api/client";
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
  });

  const cancel = useMutation({
    mutationFn: (reason: string) => cancelDocument(slug, documentId, reason),
    onSettled: () =>
      void queryClient.invalidateQueries({ queryKey: ["document", slug, documentId] }),
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
  const cancellable = canReview && CANCELLABLE_STATES.has(document.state);

  return (
    <AppShell
      title={document.original_filename}
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Documents", to: "/app/$organizationSlug/documents" },
        { label: document.original_filename },
      ]}
      actions={
        cancellable ? (
          <DialogTrigger>
            <Button variant="destructive">Cancel document</Button>
            <CancelDialog onConfirm={(reason) => cancel.mutate(reason)} />
          </DialogTrigger>
        ) : undefined
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "56rem" }}>
        {cancel.isError ? (
          <Banner tone="critical" title="Cancel failed">
            {cancel.error?.message ?? "The document was not changed."}
          </Banner>
        ) : null}
        {download.isError ? (
          <Banner tone="critical" title="Download refused">
            {download.error?.message ?? "No file was downloaded."}
          </Banner>
        ) : null}

        <Panel title="Summary">
          <dl
            style={{ display: "grid", gridTemplateColumns: "12rem 1fr", gap: "0.5rem", margin: 0 }}
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

        <Panel title="Extracted data">
          <Badge tone="neutral">not available yet — extraction arrives with processing (PRC)</Badge>
        </Panel>
        <Panel title="Validation">
          <Badge tone="neutral">not available yet — validation results arrive with PRC</Badge>
        </Panel>
        <Panel title="Delivery">
          <Badge tone="neutral">not available yet — exports arrive with EXP</Badge>
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
