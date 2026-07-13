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
import { useMemo, useState } from "react";

import {
  ApiError,
  correctField,
  fetchReviewWorkspace,
  type CorrectionResult,
  type WorkspaceField,
} from "../api/client";
import { HeaderFieldEditor, type SaveState } from "../components/review/HeaderFieldEditor";
import { DocumentViewer, type EvidenceHighlight } from "../components/viewer/DocumentViewer";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

function evidenceId(fieldKey: string, index: number): string {
  return `${fieldKey}#${index}`;
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
  //: The optimistic version corrections are authored against; each save
  //: advances it from the server's response.
  const [taskVersion, setTaskVersion] = useState<number | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [decision, setDecision] = useState<{
    route: string;
    reasons: Record<string, unknown>[];
  } | null>(null);

  const save = useMutation({
    mutationFn: ({ fieldKey, value }: { fieldKey: string; value: string }) =>
      correctField(slug, taskId, {
        field_key: fieldKey,
        value: value === "" ? null : value,
        expected_version: taskVersion ?? workspace.data?.task.version ?? 0,
      }),
    onMutate: ({ fieldKey }) =>
      setSaveStates((prev) => ({ ...prev, [fieldKey]: { status: "saving" } })),
    onSuccess: (result: CorrectionResult, { fieldKey }) => {
      setTaskVersion(result.task_version);
      setSaveStates((prev) => ({ ...prev, [fieldKey]: { status: "saved" } }));
      if (result.revalidation) setDecision(result.revalidation.decision);
      void queryClient.invalidateQueries({ queryKey: ["review-workspace", slug, taskId] });
    },
    onError: (error: unknown, { fieldKey }) => {
      const message = error instanceof Error ? error.message : "The change was not saved.";
      if (error instanceof ApiError && error.status === 409) {
        setConflict(message);
      }
      setSaveStates((prev) => ({ ...prev, [fieldKey]: { status: "error", message } }));
    },
  });

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
                  setTaskVersion(null);
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
            onSave={(fieldKey, value) => save.mutate({ fieldKey, value })}
            readOnly={!editable}
          />
        </div>
      </div>
    </AppShell>
  );
}
