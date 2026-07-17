/**
 * A single training set (extraction-training Phase 1): its labelled samples,
 * the version lifecycle (draft → published), and the picker that turns a
 * processed stream document into a labelled sample.
 *
 * Samples are ordinary documents already uploaded to the stream and processed
 * (so they carry page images + positioned text). Labelling one opens the
 * annotation view; publishing freezes the version for training and evaluation.
 */

import { Badge, Banner, Button } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";

import {
  ApiError,
  fetchDocuments,
  fetchTrainingSet,
  publishTrainingSet,
  startTrainingDraft,
  type DocumentSummary,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

// A document must be processed far enough to carry page images + geometry.
const ANNOTATABLE_STATES = new Set(["review_required", "approved", "exporting", "completed"]);

export function TrainingSet() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("streams.manage");
  const { streamSlug, trainingSlug } = useParams({ strict: false }) as {
    streamSlug: string;
    trainingSlug: string;
  };
  const queryClient = useQueryClient();

  const detail = useQuery({
    queryKey: ["training-set", slug, streamSlug, trainingSlug],
    queryFn: () => fetchTrainingSet(slug, streamSlug, trainingSlug),
  });
  const streamDocs = useQuery({
    queryKey: ["documents", slug, streamSlug],
    queryFn: () => fetchDocuments(slug, { stream: streamSlug }),
  });

  const [error, setError] = useState<string | null>(null);
  const invalidate = () =>
    void queryClient.invalidateQueries({
      queryKey: ["training-set", slug, streamSlug, trainingSlug],
    });

  const publish = useMutation({
    mutationFn: () => publishTrainingSet(slug, streamSlug, trainingSlug),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Publish failed."),
  });
  const newDraft = useMutation({
    mutationFn: () => startTrainingDraft(slug, streamSlug, trainingSlug),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not open a draft."),
  });

  if (detail.status === "error") {
    return (
      <AppShell title="Training set" breadcrumbs={[{ label: trainingSlug }]}>
        <Banner tone="critical" title="Couldn’t load this training set">
          It may have been deleted.
        </Banner>
      </AppShell>
    );
  }

  const set = detail.data;
  const samples = set?.documents ?? [];
  const sampledSourceIds = new Set(
    samples.map((doc) => doc.source_document_id).filter((id): id is string => id !== null),
  );
  const candidates = (streamDocs.data?.items ?? []).filter(
    (doc: DocumentSummary) => ANNOTATABLE_STATES.has(doc.state) && !sampledSourceIds.has(doc.id),
  );
  const sourceName = (id: string | null): string => {
    const match = (streamDocs.data?.items ?? []).find((d: DocumentSummary) => d.id === id);
    return match?.original_filename ?? (id ? `${id.slice(0, 8)}…` : "unknown");
  };

  const hasDraft = Boolean(set?.working_draft_version_id);
  const draftVersion = set?.versions.find((v) => v.id === set.working_draft_version_id);

  return (
    <AppShell
      title={set?.name ?? "Training set"}
      breadcrumbs={[
        { label: session.organization.name },
        { label: streamSlug },
        { label: "Training" },
        { label: set?.name ?? trainingSlug },
      ]}
      actions={
        <Link
          to="/app/$organizationSlug/streams/$streamSlug/training"
          params={{ organizationSlug: slug, streamSlug }}
        >
          All training sets
        </Link>
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "60rem" }}>
        <div
          style={{
            display: "flex",
            gap: "var(--soa-space-3)",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          {set?.published_version_id ? (
            <Badge tone="success">
              Published v
              {set.versions.find((v) => v.id === set.published_version_id)?.version_number}
            </Badge>
          ) : null}
          {hasDraft ? (
            <Badge tone="neutral">
              Draft v{draftVersion?.version_number} · {draftVersion?.counts.total ?? 0} samples
            </Badge>
          ) : null}
          {canManage && hasDraft ? (
            <Button
              onPress={() => publish.mutate()}
              isDisabled={publish.isPending || (draftVersion?.counts.total ?? 0) === 0}
            >
              Publish version
            </Button>
          ) : null}
          {canManage && !hasDraft ? (
            <Button onPress={() => newDraft.mutate()} isDisabled={newDraft.isPending}>
              Start a new draft
            </Button>
          ) : null}
        </div>

        {error ? (
          <Banner tone="critical" title="Action failed">
            {error}
          </Banner>
        ) : null}

        {!hasDraft && set?.published_version_id ? (
          <Banner tone="info" title="This version is published">
            Published versions are frozen. Start a new draft to keep labelling.
          </Banner>
        ) : null}

        <section style={{ display: "grid", gap: "var(--soa-space-3)" }}>
          <h2 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>
            Labelled samples ({samples.length})
          </h2>
          {samples.length === 0 ? (
            <p style={{ color: "var(--soa-text-muted)", margin: 0 }}>
              No samples yet — add a processed document below and label its fields.
            </p>
          ) : (
            <ul style={listStyle}>
              {samples.map((doc) => {
                const fieldCount = Object.keys(doc.ground_truth.fields ?? {}).length;
                const regionCount = Object.keys(doc.ground_truth.regions ?? {}).length;
                return (
                  <li key={doc.id} style={rowStyle}>
                    <span style={{ fontWeight: 600 }}>{sourceName(doc.source_document_id)}</span>
                    <Badge tone="accent">{doc.split}</Badge>
                    <span style={caption}>
                      {fieldCount} fields · {regionCount} boxes
                    </span>
                    {canManage && hasDraft && doc.source_document_id ? (
                      <Link
                        to="/app/$organizationSlug/streams/$streamSlug/training/$trainingSlug/documents/$documentId"
                        params={{
                          organizationSlug: slug,
                          streamSlug,
                          trainingSlug,
                          documentId: doc.source_document_id,
                        }}
                      >
                        Edit labels
                      </Link>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        {canManage && hasDraft ? (
          <section style={{ display: "grid", gap: "var(--soa-space-3)" }}>
            <h2 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>Add a sample</h2>
            <p style={caption}>
              Pick a processed document in this stream to label. Upload more via the stream's Upload
              page first if the list is empty.
            </p>
            {candidates.length === 0 ? (
              <p style={{ color: "var(--soa-text-muted)", margin: 0 }}>
                No unlabelled processed documents available.
              </p>
            ) : (
              <ul style={listStyle}>
                {candidates.slice(0, 25).map((doc: DocumentSummary) => (
                  <li key={doc.id} style={rowStyle}>
                    <span>{doc.original_filename}</span>
                    <Badge tone="neutral">{doc.state}</Badge>
                    <Link
                      to="/app/$organizationSlug/streams/$streamSlug/training/$trainingSlug/documents/$documentId"
                      params={{
                        organizationSlug: slug,
                        streamSlug,
                        trainingSlug,
                        documentId: doc.id,
                      }}
                    >
                      Label
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        ) : null}
      </div>
    </AppShell>
  );
}

const listStyle: React.CSSProperties = {
  listStyle: "none",
  margin: 0,
  padding: 0,
  display: "grid",
  gap: "var(--soa-space-2)",
};
const rowStyle: React.CSSProperties = {
  display: "flex",
  gap: "var(--soa-space-3)",
  alignItems: "center",
  flexWrap: "wrap",
  padding: "var(--soa-space-3)",
  border: "1px solid var(--soa-border)",
  borderRadius: "var(--soa-radius-control)",
};
const caption: React.CSSProperties = {
  font: "var(--soa-font-caption)",
  color: "var(--soa-text-muted)",
};
