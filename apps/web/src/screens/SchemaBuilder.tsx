/**
 * Schema builder (CFG-011, UI_UX_BLUEPRINT §6.4).
 *
 * Edits the extraction field tree for a process: header fields plus table
 * fields with typed columns. Reordering is keyboard-first (move up/down
 * buttons), unsaved changes are always visible, edits save with If-Match
 * optimistic concurrency (a 409 means someone else changed the draft),
 * and the server re-validates on every save and publish — an invalid
 * schema cannot be stored, let alone published.
 */

import { Badge, Banner, Button, Checkbox, Select, TextField } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";

import {
  createSchemaDraft,
  fetchSchema,
  publishSchemaVersion,
  updateSchemaDraft,
  type SchemaDefinition,
  type SchemaField,
  type SchemaVersionRecord,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const FIELD_TYPES = ["text", "number", "money", "date", "boolean", "enum", "table"].map((id) => ({
  id,
  label: id,
}));
const COLUMN_TYPES = FIELD_TYPES.filter((t) => t.id !== "table");
const CRITICALITY = ["critical", "standard", "informational"].map((id) => ({ id, label: id }));

function move<T>(items: T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  const [item] = next.splice(index, 1);
  next.splice(target, 0, item);
  return next;
}

function FieldEditor({
  field,
  onChange,
  onRemove,
  onMove,
  isColumn = false,
}: {
  field: SchemaField;
  onChange: (next: SchemaField) => void;
  onRemove: () => void;
  onMove: (delta: number) => void;
  isColumn?: boolean;
}) {
  const patch = (partial: Partial<SchemaField>) => onChange({ ...field, ...partial });
  return (
    <div
      style={{
        display: "flex",
        gap: "var(--soa-space-3)",
        alignItems: "end",
        flexWrap: "wrap",
        padding: "var(--soa-space-3)",
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-control)",
      }}
    >
      <TextField label="Key" value={field.key} onChange={(key) => patch({ key })} />
      <TextField label="Label" value={field.label} onChange={(label) => patch({ label })} />
      <Select
        label="Type"
        items={isColumn ? COLUMN_TYPES : FIELD_TYPES}
        selectedKey={field.type}
        onSelectionChange={(key) => {
          const type = String(key) as SchemaField["type"];
          patch({
            type,
            columns: type === "table" ? (field.columns ?? []) : undefined,
            enum_values: type === "enum" ? (field.enum_values ?? []) : undefined,
          });
        }}
      />
      {field.type === "enum" ? (
        <TextField
          label="Enum values (comma-separated)"
          value={(field.enum_values ?? []).join(",")}
          onChange={(raw) =>
            patch({
              enum_values: raw
                .split(",")
                .map((v) => v.trim())
                .filter(Boolean),
            })
          }
        />
      ) : null}
      <Checkbox isSelected={field.required ?? false} onChange={(required) => patch({ required })}>
        Required
      </Checkbox>
      <Select
        label="Criticality"
        items={CRITICALITY}
        selectedKey={field.criticality ?? "standard"}
        onSelectionChange={(key) =>
          patch({ criticality: String(key) as SchemaField["criticality"] })
        }
      />
      <div style={{ display: "flex", gap: "var(--soa-space-1)" }}>
        <Button
          size="sm"
          variant="subtle"
          aria-label={`Move ${field.key || "field"} up`}
          onPress={() => onMove(-1)}
        >
          ↑
        </Button>
        <Button
          size="sm"
          variant="subtle"
          aria-label={`Move ${field.key || "field"} down`}
          onPress={() => onMove(1)}
        >
          ↓
        </Button>
        <Button
          size="sm"
          variant="destructive"
          aria-label={`Remove ${field.key || "field"}`}
          onPress={onRemove}
        >
          Remove
        </Button>
      </div>
    </div>
  );
}

const NEW_FIELD: SchemaField = { key: "", label: "", type: "text", criticality: "standard" };

export function SchemaBuilder() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("processes.manage");
  const { processSlug } = useParams({ strict: false }) as { processSlug: string };
  const queryClient = useQueryClient();

  const schema = useQuery({
    queryKey: ["schema", slug, processSlug],
    queryFn: () => fetchSchema(slug, processSlug),
  });

  const versions: SchemaVersionRecord[] = schema.data?.versions ?? [];
  const draft = [...versions].reverse().find((v) => v.state === "draft");
  const published = [...versions].reverse().find((v) => v.state === "published");
  const baseline: SchemaDefinition = useMemo(
    () => draft?.definition ?? published?.definition ?? { fields: [] },
    [draft, published],
  );

  const [fields, setFields] = useState<SchemaField[]>(baseline.fields);
  useEffect(() => setFields(baseline.fields), [baseline]);
  const dirty = JSON.stringify(fields) !== JSON.stringify(baseline.fields);

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ["schema", slug, processSlug] });
  const save = useMutation({
    mutationFn: () =>
      draft
        ? updateSchemaDraft(slug, processSlug, draft.id, draft.version, { fields })
        : createSchemaDraft(slug, processSlug, { fields }),
    onSuccess: invalidate,
  });
  const publish = useMutation({
    mutationFn: (versionId: string) => publishSchemaVersion(slug, processSlug, versionId),
    onSuccess: invalidate,
  });

  const setField = (index: number, next: SchemaField) =>
    setFields((current) => current.map((f, i) => (i === index ? next : f)));

  if (schema.status === "error") {
    return (
      <AppShell title="Schema" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load the schema"
          action={
            <Button size="sm" onPress={() => void schema.refetch()}>
              Try again
            </Button>
          }
        >
          Nothing has been changed.
        </Banner>
      </AppShell>
    );
  }

  return (
    <AppShell
      title="Extraction schema"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Processes", to: "/app/$organizationSlug/processes" },
        { label: processSlug },
        { label: "Schema" },
      ]}
      actions={
        canManage ? (
          <div style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "center" }}>
            {dirty ? <Badge tone="warning">Unsaved changes</Badge> : null}
            <Button onPress={() => save.mutate()} isDisabled={!dirty || save.isPending}>
              {draft ? "Save draft" : "Create draft"}
            </Button>
            {draft ? (
              <Button
                variant="secondary"
                isDisabled={dirty || publish.isPending}
                onPress={() => publish.mutate(draft.id)}
              >
                Publish
              </Button>
            ) : null}
          </div>
        ) : undefined
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
        {save.isError ? (
          <Banner tone="critical" title="The schema was not saved">
            {save.error?.message ?? "Validation failed."}
          </Banner>
        ) : null}
        {publish.isError ? (
          <Banner tone="critical" title="Publish refused">
            {publish.error?.message ?? "The draft did not validate."}
          </Banner>
        ) : null}
        {published && !draft && !dirty ? (
          <Banner tone="info" title={`Editing starts from published v${published.version_number}`}>
            Your first change creates a new draft; the published version stays untouched.
          </Banner>
        ) : null}

        <div style={{ display: "grid", gap: "var(--soa-space-3)" }}>
          {fields.map((field, index) => (
            <div key={index} style={{ display: "grid", gap: "var(--soa-space-2)" }}>
              <FieldEditor
                field={field}
                onChange={(next) => setField(index, next)}
                onRemove={() => setFields((current) => current.filter((_, i) => i !== index))}
                onMove={(delta) => setFields((current) => move(current, index, delta))}
              />
              {field.type === "table" ? (
                <div
                  style={{
                    marginLeft: "var(--soa-space-6)",
                    display: "grid",
                    gap: "var(--soa-space-2)",
                  }}
                >
                  {(field.columns ?? []).map((column, columnIndex) => (
                    <FieldEditor
                      key={columnIndex}
                      field={column}
                      isColumn
                      onChange={(next) =>
                        setField(index, {
                          ...field,
                          columns: (field.columns ?? []).map((c, i) =>
                            i === columnIndex ? next : c,
                          ),
                        })
                      }
                      onRemove={() =>
                        setField(index, {
                          ...field,
                          columns: (field.columns ?? []).filter((_, i) => i !== columnIndex),
                        })
                      }
                      onMove={(delta) =>
                        setField(index, {
                          ...field,
                          columns: move(field.columns ?? [], columnIndex, delta),
                        })
                      }
                    />
                  ))}
                  <div>
                    <Button
                      size="sm"
                      variant="subtle"
                      onPress={() =>
                        setField(index, {
                          ...field,
                          columns: [...(field.columns ?? []), { ...NEW_FIELD }],
                        })
                      }
                    >
                      Add column
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>
          ))}
          <div>
            <Button onPress={() => setFields((current) => [...current, { ...NEW_FIELD }])}>
              Add field
            </Button>
          </div>
        </div>

        <section aria-label="Schema preview">
          <h2 style={{ font: "var(--soa-font-heading-md)" }}>Preview</h2>
          <pre
            style={{
              background: "var(--soa-surface-sunken, var(--soa-surface))",
              border: "1px solid var(--soa-border)",
              borderRadius: "var(--soa-radius-panel)",
              padding: "var(--soa-space-4)",
              overflowX: "auto",
              font: "var(--soa-font-caption)",
            }}
          >
            {JSON.stringify({ fields }, null, 2)}
          </pre>
        </section>
      </div>
    </AppShell>
  );
}
