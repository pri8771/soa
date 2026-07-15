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
import { useEffect, useMemo, useState } from "react";

import {
  createInstructionDraft,
  fetchInstructionVersions,
  publishInstructionVersion,
  updateInstructionDraft,
  type InstructionVersion,
} from "../../api/client";

function guidanceJson(value: Record<string, string>): string {
  return JSON.stringify(value, null, 2);
}

export function InstructionEditor({
  organizationSlug,
  streamVersionId,
  schemaVersionId,
  canManage,
}: {
  organizationSlug: string;
  streamVersionId: string;
  schemaVersionId: string;
  canManage: boolean;
}) {
  const queryClient = useQueryClient();
  const versions = useQuery({
    queryKey: ["instruction-versions", organizationSlug, streamVersionId],
    queryFn: () => fetchInstructionVersions(organizationSlug, streamVersionId),
  });
  const working = useMemo<InstructionVersion | null>(() => {
    const records = versions.data?.items ?? [];
    return records.find((record) => record.state === "draft") ?? records.at(-1) ?? null;
  }, [versions.data?.items]);
  const [instructions, setInstructions] = useState("");
  const [fieldGuidance, setFieldGuidance] = useState("{}");
  const [changeSummary, setChangeSummary] = useState("");
  const [dirty, setDirty] = useState(false);
  const [jsonError, setJsonError] = useState<string | null>(null);

  useEffect(() => {
    setInstructions(working?.content.instructions ?? "");
    setFieldGuidance(guidanceJson(working?.content.field_guidance ?? {}));
    setChangeSummary(working?.change_summary ?? "");
    setDirty(false);
    setJsonError(null);
    // Same-record background refetches must not erase an operator's unsaved
    // edits. Re-seed only when the selected version changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [working?.id]);

  const content = (): InstructionVersion["content"] | null => {
    try {
      const parsed = JSON.parse(fieldGuidance) as unknown;
      if (
        !parsed ||
        Array.isArray(parsed) ||
        typeof parsed !== "object" ||
        Object.values(parsed).some((value) => typeof value !== "string")
      ) {
        throw new Error("Field guidance must be a JSON object whose values are strings.");
      }
      setJsonError(null);
      return { instructions, field_guidance: parsed as Record<string, string> };
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : "Field guidance JSON is invalid.");
      return null;
    }
  };
  const invalidate = () =>
    queryClient.invalidateQueries({
      queryKey: ["instruction-versions", organizationSlug, streamVersionId],
    });
  const create = useMutation({
    mutationFn: () => {
      const next = content();
      if (!next) throw new Error("Fix field guidance before creating a draft.");
      return createInstructionDraft(
        organizationSlug,
        streamVersionId,
        schemaVersionId,
        next,
        changeSummary.trim() || null,
      );
    },
    onSuccess: invalidate,
  });
  const save = useMutation({
    mutationFn: () => {
      if (!working || working.state !== "draft") throw new Error("Create a draft first.");
      const next = content();
      if (!next) throw new Error("Fix field guidance before saving.");
      return updateInstructionDraft(
        organizationSlug,
        working.id,
        next,
        changeSummary.trim() || null,
      );
    },
    onSuccess: () => {
      setDirty(false);
      void invalidate();
    },
  });
  const publish = useMutation({
    mutationFn: () => {
      if (!working || working.state !== "draft") throw new Error("Create a draft first.");
      return publishInstructionVersion(organizationSlug, working.id);
    },
    onSuccess: invalidate,
  });
  // A brand-new instruction needs to be editable before its first draft can
  // exist. Published/superseded versions remain immutable until the operator
  // explicitly creates the next draft.
  const readOnly = !canManage || (working !== null && working.state !== "draft");
  const failed = create.error ?? save.error ?? publish.error;

  return (
    <section
      aria-label="Extraction instructions"
      style={{
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
        padding: "var(--soa-space-5)",
        display: "grid",
        gap: "var(--soa-space-4)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--soa-space-3)",
          flexWrap: "wrap",
        }}
      >
        <h2 style={{ margin: 0, font: "var(--soa-font-heading-md)" }}>Extraction instructions</h2>
        <Badge tone="neutral">stream version {streamVersionId.slice(0, 8)}</Badge>
        {working ? (
          <Badge tone={working.state === "published" ? "success" : "info"}>
            v{working.version_number} · {working.state}
          </Badge>
        ) : null}
        {dirty ? <Badge tone="warning">unsaved</Badge> : null}
        <span style={{ flex: 1 }} />
        {canManage && (!working || working.state !== "draft") ? (
          <Button
            size="sm"
            onPress={() => create.mutate()}
            isDisabled={create.isPending || (!working && !instructions.trim())}
          >
            Create instruction draft
          </Button>
        ) : null}
        {!readOnly ? (
          <>
            <Button
              size="sm"
              variant="primary"
              isDisabled={!dirty || save.isPending}
              onPress={() => save.mutate()}
            >
              Save instructions
            </Button>
            <DialogTrigger>
              <Button size="sm" isDisabled={dirty || publish.isPending}>
                Publish instructions
              </Button>
              <Dialog title="Publish extraction instructions?">
                {({ close }) => (
                  <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
                    <p style={{ margin: 0 }}>
                      Version {working?.version_number} becomes immutable and will be referenced by
                      future extraction runs for this stream version.
                    </p>
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "flex-end",
                        gap: "var(--soa-space-2)",
                      }}
                    >
                      <Button variant="subtle" onPress={close}>
                        Keep draft
                      </Button>
                      <Button
                        variant="primary"
                        onPress={() => {
                          publish.mutate();
                          close();
                        }}
                      >
                        Publish immutable version
                      </Button>
                    </div>
                  </div>
                )}
              </Dialog>
            </DialogTrigger>
          </>
        ) : null}
      </div>
      <Banner tone="warning" title="Sensitive production configuration">
        Published instructions are immutable and referenced by extraction runs. Do not place
        secrets, credentials, or untrusted document content in these fields.
      </Banner>
      {versions.status === "pending" ? <Skeleton height="8rem" /> : null}
      {versions.status === "error" ? (
        <Banner tone="critical" title="Couldn’t load extraction instructions">
          {versions.error.message}
        </Banner>
      ) : null}
      {failed ? (
        <Banner tone="critical" title="Instruction change failed">
          {failed.message}
        </Banner>
      ) : null}
      {working || canManage ? (
        <>
          <label style={{ display: "grid", gap: "var(--soa-space-1)" }}>
            Instruction text
            <textarea
              value={instructions}
              readOnly={readOnly}
              rows={8}
              onChange={(event) => {
                setInstructions(event.target.value);
                setDirty(true);
              }}
            />
          </label>
          <label style={{ display: "grid", gap: "var(--soa-space-1)" }}>
            Field guidance (JSON object)
            <textarea
              value={fieldGuidance}
              readOnly={readOnly}
              rows={6}
              aria-invalid={Boolean(jsonError)}
              onChange={(event) => {
                setFieldGuidance(event.target.value);
                setDirty(true);
                setJsonError(null);
              }}
            />
          </label>
          {jsonError ? <p role="alert">{jsonError}</p> : null}
          <TextField
            label="Change summary"
            value={changeSummary}
            onChange={(value) => {
              setChangeSummary(value);
              setDirty(true);
            }}
            isReadOnly={readOnly}
          />
          {working?.reference ? (
            <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
              Run reference: <code>{working.reference}</code>
            </p>
          ) : null}
        </>
      ) : (
        <p>No extraction instructions exist for this stream version.</p>
      )}
    </section>
  );
}
