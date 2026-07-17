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
  compileTrainingExamples,
  evaluateTrainingSet,
  fetchDocuments,
  fetchTrainingEvaluations,
  fetchTrainingSet,
  publishInstructionVersion,
  publishTrainingSet,
  startTrainingDraft,
  type DocumentSummary,
  type TrainingEvaluation,
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

  const evaluations = useQuery({
    queryKey: ["training-evaluations", slug, streamSlug, trainingSlug],
    queryFn: () => fetchTrainingEvaluations(slug, streamSlug, trainingSlug),
    // While a run is scoring, poll so the per-field result appears when ready.
    refetchInterval: 5000,
  });
  const [notice, setNotice] = useState<string | null>(null);
  const compile = useMutation({
    // Compile few-shot exemplars from the published train-split samples, then
    // publish the resulting instruction version so it goes live for extraction.
    mutationFn: async () => {
      const compiled = await compileTrainingExamples(slug, streamSlug, trainingSlug);
      await publishInstructionVersion(slug, compiled.instruction_version_id);
      return compiled;
    },
    onSuccess: (compiled) => {
      setError(null);
      setNotice(
        `Compiled ${compiled.example_count} few-shot example${
          compiled.example_count === 1 ? "" : "s"
        } and published them live (${compiled.reference}).`,
      );
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Compile failed."),
  });
  const evaluate = useMutation({
    mutationFn: () =>
      evaluateTrainingSet(slug, streamSlug, trainingSlug, { execution_mode: "server" }),
    onSuccess: () => {
      setError(null);
      setNotice("Evaluation queued — per-field accuracy will appear below when it finishes.");
      void queryClient.invalidateQueries({
        queryKey: ["training-evaluations", slug, streamSlug, trainingSlug],
      });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not evaluate."),
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

        {notice ? (
          <Banner tone="success" title="Done">
            {notice}
          </Banner>
        ) : null}

        {canManage && set?.published_version_id ? (
          <section style={{ display: "grid", gap: "var(--soa-space-3)" }}>
            <h2 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>Train &amp; measure</h2>
            <div style={{ display: "flex", gap: "var(--soa-space-3)", flexWrap: "wrap" }}>
              <Button onPress={() => compile.mutate()} isDisabled={compile.isPending}>
                Compile few-shot examples &amp; publish
              </Button>
              <Button
                variant="subtle"
                onPress={() => evaluate.mutate()}
                isDisabled={evaluate.isPending}
              >
                Evaluate on held-out slice
              </Button>
            </div>
            <p style={caption}>
              Compiling turns the published train-split samples into few-shot exemplars and
              publishes them live for this stream. Evaluating scores the current config against the
              set&apos;s held-out (validation/test) samples; run it before and after training to see
              the per-field lift.
            </p>
            <EvaluationList items={evaluations.data?.items ?? []} />
          </section>
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

function pct(rate: number): string {
  return `${Math.round(rate * 100)}%`;
}

function EvaluationList({ items }: { items: TrainingEvaluation[] }) {
  if (items.length === 0) {
    return <p style={caption}>No evaluations yet.</p>;
  }
  return (
    <ul style={listStyle}>
      {items.map((run) => {
        const fields = Object.entries(run.by_field);
        return (
          <li
            key={run.id}
            style={{ ...rowStyle, alignItems: "flex-start", flexDirection: "column" }}
          >
            <div
              style={{
                display: "flex",
                gap: "var(--soa-space-3)",
                alignItems: "center",
                flexWrap: "wrap",
              }}
            >
              <Badge
                tone={
                  run.state === "succeeded"
                    ? "success"
                    : run.state === "failed"
                      ? "critical"
                      : "neutral"
                }
              >
                {run.state}
              </Badge>
              <span style={caption}>{run.execution_mode}</span>
              {run.state === "succeeded" ? (
                <Badge tone={run.promotion_eligible ? "success" : "warning"}>
                  {run.promotion_eligible ? "Gate: promotable" : "Gate: blocked"}
                </Badge>
              ) : null}
              {run.safe_error ? <span style={caption}>{run.safe_error}</span> : null}
            </div>
            {fields.length > 0 ? (
              <div style={{ display: "grid", gap: 2, width: "100%" }}>
                {fields.map(([key, score]) => (
                  <div
                    key={key}
                    style={{
                      display: "flex",
                      gap: "var(--soa-space-2)",
                      font: "var(--soa-font-caption)",
                    }}
                  >
                    <span style={{ minWidth: "10rem" }}>{key}</span>
                    <span>
                      {score.exact}/{score.total} exact (
                      {pct(score.total ? score.exact / score.total : 0)})
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
            {run.field_diffs.length > 0 ? (
              <div style={{ display: "grid", gap: 2, width: "100%" }}>
                <span style={{ ...caption, fontWeight: 600 }}>Before → after</span>
                {run.field_diffs.map((diff) => (
                  <div
                    key={diff.field}
                    style={{
                      display: "flex",
                      gap: "var(--soa-space-2)",
                      font: "var(--soa-font-caption)",
                    }}
                  >
                    <span style={{ minWidth: "10rem" }}>{diff.field}</span>
                    <span>
                      {pct(diff.current_exact_rate)} → {pct(diff.candidate_exact_rate)} (
                      {diff.delta >= 0 ? "+" : ""}
                      {Math.round(diff.delta * 100)} pts)
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
          </li>
        );
      })}
    </ul>
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
