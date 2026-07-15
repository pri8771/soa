/**
 * Mapping studio (EXP-004): edit an integration's mapping profile.
 *
 * The editor is a row-per-target grid over the canonical order: source
 * path (datalist over the canonical schema's paths, free entry allowed),
 * transform with its parameter, default, and required flag — plus
 * constants and the line-items section. Everything is native
 * inputs/selects/buttons, so the whole studio is keyboard accessible.
 *
 * Validation runs on the SERVER against a sample canonical order and
 * returns the mapped payload with its trace; every returned error names
 * its mapping row ($.fields[i]) and the studio pins it to that row
 * (aria-invalid + inline message). Publishing is disabled while there
 * are unsaved changes, and the working version's state is always on
 * screen — published mappings are read-only with a "new draft" path.
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
import { useParams } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import {
  activateIntegration,
  archiveIntegration,
  createMappingDraft,
  deactivateIntegration,
  fetchIntegrationDetail,
  publishMappingVersion,
  setIntegrationCredential,
  testIntegrationConnection,
  updateMappingDraft,
  validateMappingVersion,
  type MappingDefinition,
  type MappingFieldSpec,
  type MappingValidationResult,
  type MappingVersionRecord,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

//: Canonical source paths (header scope) — free entry stays possible.
const HEADER_SOURCES = [
  "identifiers.po_number",
  "identifiers.sales_order_reference",
  "dates.order_date",
  "dates.requested_delivery_date",
  "terms.currency",
  "terms.payment_terms",
  "totals.grand_total.amount",
  "totals.subtotal.amount",
  "parties.buyer.name",
  "source.document_id",
  "source.run_id",
];
const LINE_SOURCES = [
  "line_number",
  "sku",
  "description",
  "quantity",
  "unit_of_measure",
  "unit_price.amount",
  "line_total.amount",
];

function CredentialDialog({
  configured,
  isPending,
  error,
  onSave,
}: {
  configured: boolean;
  isPending: boolean;
  error: Error | null;
  onSave: (kind: string, secret: string, onSuccess: () => void) => void;
}) {
  const [kind, setKind] = useState("bearer_token");
  const [secret, setSecret] = useState("");
  return (
    <Dialog title={configured ? "Rotate credential" : "Configure credential"}>
      {({ close }) => (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onSave(kind.trim(), secret, () => {
              // A write-only secret must not remain in React state after the
              // server accepts it; reopening the dialog always starts blank.
              setSecret("");
              close();
            });
          }}
          style={{ display: "grid", gap: "var(--soa-space-4)", minWidth: "min(28rem, 80vw)" }}
        >
          <Banner tone="warning" title="Write-only secret">
            The value is sent once to the secret store and is never returned by the UI or API.
            Rotating queues the previous credential for durable post-commit revocation.
          </Banner>
          <TextField label="Credential kind" value={kind} onChange={setKind} isRequired />
          <TextField
            label="Secret value"
            type="password"
            autoComplete="new-password"
            value={secret}
            onChange={setSecret}
            isRequired
          />
          {error ? (
            <Banner tone="critical" title="Credential wasn’t stored">
              {error.message}
            </Banner>
          ) : null}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "var(--soa-space-2)" }}>
            <Button
              variant="subtle"
              onPress={() => {
                setSecret("");
                close();
              }}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              isDisabled={kind.trim().length < 1 || secret.length < 8 || isPending}
            >
              {configured ? "Rotate credential" : "Store credential"}
            </Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

//: Transforms the studio edits directly; value maps stay JSON-level
//: (the API accepts them; the form keeps to single-parameter kinds).
const TRANSFORMS = ["", "uppercase", "lowercase", "trim", "decimal", "date", "prefix", "suffix"];

function transformParam(kind: string): { key: string; label: string } | null {
  if (kind === "decimal") return { key: "scale", label: "Scale" };
  if (kind === "date") return { key: "pattern", label: "Pattern (YYYY/MM/DD tokens)" };
  if (kind === "prefix" || kind === "suffix") return { key: "value", label: "Text" };
  return null;
}

interface RowError {
  scope: "fields" | "lines";
  index: number;
  message: string;
}

function parseRowErrors(errors: string[]): { rows: RowError[]; general: string[] } {
  const rows: RowError[] = [];
  const general: string[] = [];
  for (const message of errors) {
    const fieldMatch = /^\$\.fields\[(\d+)\]/.exec(message);
    const lineMatch = /^\$\.lines\.fields\[(\d+)\]/.exec(message);
    if (fieldMatch) rows.push({ scope: "fields", index: Number(fieldMatch[1]), message });
    else if (lineMatch) rows.push({ scope: "lines", index: Number(lineMatch[1]), message });
    else general.push(message);
  }
  return { rows, general };
}

function FieldRow({
  spec,
  index,
  scope,
  errors,
  readOnly,
  onChange,
  onRemove,
}: {
  spec: MappingFieldSpec;
  index: number;
  scope: "fields" | "lines";
  errors: string[];
  readOnly: boolean;
  onChange: (next: MappingFieldSpec) => void;
  onRemove: () => void;
}) {
  const kind = String(spec.format?.["kind"] ?? "");
  const param = transformParam(kind);
  const rowLabel = `${scope === "lines" ? "Line mapping" : "Mapping"} row ${index + 1}`;
  return (
    <li
      aria-label={rowLabel}
      aria-invalid={errors.length > 0}
      style={{
        display: "grid",
        gap: "var(--soa-space-2)",
        border: `1px solid ${errors.length > 0 ? "var(--soa-critical, #c33)" : "var(--soa-border)"}`,
        borderRadius: "var(--soa-radius-md)",
        padding: "var(--soa-space-3)",
      }}
    >
      <div
        style={{ display: "flex", gap: "var(--soa-space-2)", flexWrap: "wrap", alignItems: "end" }}
      >
        <TextField
          label="Target field"
          value={spec.target}
          onChange={(value) => onChange({ ...spec, target: value })}
          isReadOnly={readOnly}
        />
        <label style={{ display: "grid", gap: "0.2rem", font: "var(--soa-font-caption)" }}>
          Source path
          <input
            list={`${scope}-sources`}
            value={spec.source}
            readOnly={readOnly}
            onChange={(event) => onChange({ ...spec, source: event.target.value })}
            style={{ font: "var(--soa-font-body-sm)", padding: "0.35rem" }}
          />
        </label>
        <label style={{ display: "grid", gap: "0.2rem", font: "var(--soa-font-caption)" }}>
          Transform
          <select
            value={kind}
            disabled={readOnly}
            onChange={(event) => {
              const next = event.target.value;
              onChange({ ...spec, format: next === "" ? undefined : { kind: next } });
            }}
            style={{ font: "var(--soa-font-body-sm)", padding: "0.35rem" }}
          >
            {TRANSFORMS.map((value) => (
              <option key={value} value={value}>
                {value === "" ? "none" : value}
              </option>
            ))}
          </select>
        </label>
        {param ? (
          <TextField
            label={param.label}
            value={String(spec.format?.[param.key] ?? "")}
            onChange={(value) =>
              onChange({
                ...spec,
                format: {
                  kind,
                  [param.key]: param.key === "scale" ? Number(value) : value,
                },
              })
            }
            isReadOnly={readOnly}
          />
        ) : null}
        <TextField
          label="Default"
          value={spec.default === undefined ? "" : String(spec.default)}
          onChange={(value) => onChange({ ...spec, default: value === "" ? undefined : value })}
          isReadOnly={readOnly}
        />
        <label
          style={{
            display: "flex",
            gap: "0.3rem",
            alignItems: "center",
            font: "var(--soa-font-caption)",
          }}
        >
          <input
            type="checkbox"
            checked={Boolean(spec.required)}
            disabled={readOnly}
            onChange={(event) =>
              onChange({ ...spec, required: event.target.checked ? true : undefined })
            }
          />
          required
        </label>
        {!readOnly ? (
          <Button size="sm" variant="subtle" onPress={onRemove}>
            {`Remove row ${index + 1}`}
          </Button>
        ) : null}
      </div>
      {errors.map((message) => (
        <p key={message} role="alert" style={{ margin: 0, color: "var(--soa-critical, #c33)" }}>
          {message}
        </p>
      ))}
    </li>
  );
}

export function MappingStudio() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const { integrationSlug } = useParams({ strict: false }) as { integrationSlug: string };
  const queryClient = useQueryClient();
  const canManage = session.permissions.has("integrations.manage");
  const canManageCredentials = session.permissions.has("credentials.manage");

  const detail = useQuery({
    queryKey: ["integration", slug, integrationSlug],
    queryFn: () => fetchIntegrationDetail(slug, integrationSlug),
  });

  //: The working copy: seeded from the working version on first render
  //: of data (keyed remount below), edited locally, saved with If-Match.
  const [draft, setDraft] = useState<{
    definition: MappingDefinition;
    requiredTargets: string;
  } | null>(null);
  const [dirty, setDirty] = useState(false);
  const [validation, setValidation] = useState<MappingValidationResult | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const versions = detail.data?.mapping_versions ?? [];
  const working: MappingVersionRecord | null =
    versions.find((record) => record.state === "draft") ?? versions.at(-1) ?? null;
  const readOnly = !canManage || working === null || working.state !== "draft";

  const effectiveDefinition: MappingDefinition = useMemo(
    () => draft?.definition ?? working?.definition ?? { fields: [], constants: [] },
    [draft?.definition, working?.definition],
  );
  const requiredTargets =
    draft?.requiredTargets ??
    ((working?.target_schema["required"] as string[] | undefined) ?? []).join(", ");

  const edit = (next: MappingDefinition) => {
    setDraft({ definition: next, requiredTargets });
    setDirty(true);
    setValidation(null);
  };

  const targetSchema = useMemo(() => {
    const required = requiredTargets
      .split(",")
      .map((entry) => entry.trim())
      .filter(Boolean);
    return required.length > 0 ? { type: "object", required } : {};
  }, [requiredTargets]);

  const producedTargets = useMemo(() => {
    const targets = new Set<string>();
    for (const spec of effectiveDefinition.fields ?? []) targets.add(spec.target);
    for (const spec of effectiveDefinition.constants ?? []) targets.add(spec.target);
    return targets;
  }, [effectiveDefinition]);

  const save = useMutation({
    mutationFn: () => {
      if (working === null) throw new Error("no draft");
      return updateMappingDraft(slug, integrationSlug, working.id, working.version, {
        definition: effectiveDefinition,
        target_schema: targetSchema,
      });
    },
    onSuccess: () => {
      setDirty(false);
      setMessage("Draft saved.");
      void queryClient.invalidateQueries({ queryKey: ["integration", slug, integrationSlug] });
    },
    onError: (error: unknown) =>
      setMessage(error instanceof Error ? `Save failed: ${error.message}` : "Save failed."),
  });

  const newDraft = useMutation({
    mutationFn: () =>
      createMappingDraft(slug, integrationSlug, {
        definition: working?.definition ?? { fields: [], constants: [] },
        target_schema: working?.target_schema ?? {},
      }),
    onSuccess: () => {
      setDraft(null);
      setDirty(false);
      setMessage("New draft created from the latest version.");
      void queryClient.invalidateQueries({ queryKey: ["integration", slug, integrationSlug] });
    },
  });

  const validate = useMutation({
    mutationFn: () => {
      if (working === null) throw new Error("no draft");
      return validateMappingVersion(slug, integrationSlug, working.id);
    },
    onSuccess: (result) => {
      setValidation(result);
      setMessage(result.valid ? "Mapping is valid — preview below." : "Validation found problems.");
    },
  });

  const publish = useMutation({
    mutationFn: () => {
      if (working === null) throw new Error("no draft");
      return publishMappingVersion(slug, integrationSlug, working.id);
    },
    onSuccess: () => {
      setMessage("Mapping published.");
      void queryClient.invalidateQueries({ queryKey: ["integration", slug, integrationSlug] });
    },
    onError: (error: unknown) =>
      setMessage(error instanceof Error ? `Publish failed: ${error.message}` : "Publish failed."),
  });
  const credential = useMutation({
    mutationFn: ({ kind, secret }: { kind: string; secret: string }) =>
      setIntegrationCredential(slug, integrationSlug, kind, secret),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["integration", slug, integrationSlug] }),
  });
  const connectionTest = useMutation({
    mutationFn: () => testIntegrationConnection(slug, integrationSlug),
    onSuccess: (result) => setMessage(result.detail),
    onError: (error: unknown) =>
      setMessage(
        error instanceof Error ? `Connection test failed: ${error.message}` : "Connection failed.",
      ),
  });
  const activate = useMutation({
    mutationFn: () => activateIntegration(slug, detail.data!.integration),
    onSuccess: (result) => {
      setMessage(result.detail);
      void queryClient.invalidateQueries({ queryKey: ["integration", slug, integrationSlug] });
      void queryClient.invalidateQueries({ queryKey: ["integrations", slug] });
    },
  });
  const deactivate = useMutation({
    mutationFn: () => deactivateIntegration(slug, detail.data!.integration),
    onSuccess: () => {
      setMessage("Integration paused; new delivery attempts are blocked.");
      void queryClient.invalidateQueries({ queryKey: ["integration", slug, integrationSlug] });
      void queryClient.invalidateQueries({ queryKey: ["integrations", slug] });
    },
  });
  const archive = useMutation({
    mutationFn: () => archiveIntegration(slug, detail.data!.integration),
    onSuccess: () => {
      setMessage("Integration archived.");
      void queryClient.invalidateQueries({ queryKey: ["integration", slug, integrationSlug] });
      void queryClient.invalidateQueries({ queryKey: ["integrations", slug] });
    },
  });

  if (detail.status === "pending") {
    return (
      <AppShell title="Mapping studio" breadcrumbs={[{ label: session.organization.name }]}>
        <Skeleton height="16rem" />
      </AppShell>
    );
  }
  if (detail.status === "error") {
    return (
      <AppShell title="Mapping studio" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner tone="critical" title="Couldn’t load this integration">
          It may have been removed, or the service did not respond.
        </Banner>
      </AppShell>
    );
  }

  const integration = detail.data.integration;
  const rowErrors = validation ? parseRowErrors(validation.errors) : { rows: [], general: [] };
  const errorsFor = (scope: "fields" | "lines", index: number) =>
    rowErrors.rows
      .filter((entry) => entry.scope === scope && entry.index === index)
      .map((entry) => entry.message);
  const fields = effectiveDefinition.fields ?? [];
  const constants = effectiveDefinition.constants ?? [];
  const lineFields = effectiveDefinition.lines?.fields ?? [];

  return (
    <AppShell
      title={`Mapping: ${integration.name}`}
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Integrations", to: "/app/$organizationSlug/integrations" },
        { label: integration.name },
      ]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-4)", maxWidth: "64rem" }}>
        <div
          style={{
            display: "flex",
            gap: "var(--soa-space-2)",
            alignItems: "center",
            flexWrap: "wrap",
          }}
        >
          <Badge tone={integration.status === "active" ? "success" : "warning"}>
            integration {integration.status}
          </Badge>
          <Badge tone={integration.credential_configured ? "success" : "warning"}>
            {integration.credential_configured ? "credential configured" : "credential required"}
          </Badge>
          {working ? (
            <>
              <Badge
                tone={
                  working.state === "published"
                    ? "success"
                    : working.state === "draft"
                      ? "info"
                      : "neutral"
                }
              >
                {`v${working.version_number} — ${working.state}`}
              </Badge>
              {dirty ? <Badge tone="warning">unsaved changes</Badge> : null}
            </>
          ) : (
            <Badge tone="neutral">no mapping versions yet</Badge>
          )}
          <span style={{ flex: 1 }} />
          {canManageCredentials ? (
            <DialogTrigger>
              <Button size="sm" variant="subtle">
                {integration.credential_configured ? "Rotate credential" : "Configure credential"}
              </Button>
              <CredentialDialog
                configured={integration.credential_configured}
                isPending={credential.isPending}
                error={credential.error}
                onSave={(kind, secret, onSuccess) =>
                  credential.mutate({ kind, secret }, { onSuccess })
                }
              />
            </DialogTrigger>
          ) : null}
          {canManageCredentials && integration.credential_configured && integration.endpoint_url ? (
            <Button
              size="sm"
              variant="subtle"
              isDisabled={connectionTest.isPending}
              onPress={() => connectionTest.mutate()}
            >
              Test connection
            </Button>
          ) : null}
          {canManage && integration.status === "paused" ? (
            <Button
              size="sm"
              variant="primary"
              isDisabled={
                !canManageCredentials ||
                !integration.production_ready ||
                !integration.credential_configured ||
                !integration.active_mapping_version_id ||
                activate.isPending
              }
              onPress={() => activate.mutate()}
            >
              Activate
            </Button>
          ) : null}
          {canManage && integration.status === "active" ? (
            <Button size="sm" variant="subtle" onPress={() => deactivate.mutate()}>
              Pause delivery
            </Button>
          ) : null}
          {canManage && integration.status !== "archived" ? (
            <DialogTrigger>
              <Button size="sm" variant="destructive">
                Archive
              </Button>
              <Dialog title="Archive integration?" alert>
                {({ close }) => (
                  <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
                    <p style={{ margin: 0 }}>
                      Archiving is irreversible and blocks every future delivery attempt.
                    </p>
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "flex-end",
                        gap: "var(--soa-space-2)",
                      }}
                    >
                      <Button variant="subtle" onPress={close}>
                        Keep integration
                      </Button>
                      <Button
                        variant="destructive"
                        isDisabled={archive.isPending}
                        onPress={() => archive.mutate(undefined, { onSuccess: close })}
                      >
                        Archive integration
                      </Button>
                    </div>
                  </div>
                )}
              </Dialog>
            </DialogTrigger>
          ) : null}
          {canManage && (working === null || working.state !== "draft") ? (
            <Button size="sm" onPress={() => newDraft.mutate()}>
              New draft
            </Button>
          ) : null}
          {!readOnly ? (
            <>
              <Button size="sm" variant="primary" isDisabled={!dirty} onPress={() => save.mutate()}>
                Save draft
              </Button>
              <Button size="sm" isDisabled={dirty} onPress={() => validate.mutate()}>
                Validate with sample
              </Button>
              <Button
                size="sm"
                isDisabled={dirty || publish.isPending}
                onPress={() => publish.mutate()}
              >
                Publish
              </Button>
            </>
          ) : null}
        </div>
        <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
          Destination: {integration.endpoint_url ?? "not configured"}. “Validate with sample” is a
          read-only mapping execution and does not contact the destination.
        </p>
        {!integration.production_ready ? (
          <Banner tone="critical" title="Production delivery is disabled">
            {integration.readiness_detail} A successful connection test proves reachability only; it
            does not enable order delivery.
          </Banner>
        ) : null}
        {dirty ? (
          <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
            Save the draft to enable validation and publishing.
          </p>
        ) : null}
        <div role="status" aria-live="polite">
          {message ? <p style={{ margin: 0 }}>{message}</p> : null}
        </div>

        <datalist id="fields-sources">
          {HEADER_SOURCES.map((path) => (
            <option key={path} value={path} />
          ))}
        </datalist>
        <datalist id="lines-sources">
          {LINE_SOURCES.map((path) => (
            <option key={path} value={path} />
          ))}
        </datalist>

        <section
          aria-label="Required targets"
          style={{ display: "grid", gap: "var(--soa-space-2)" }}
        >
          <TextField
            label="Required target fields (comma-separated; becomes the target schema)"
            value={requiredTargets}
            onChange={(value) => {
              setDraft({ definition: effectiveDefinition, requiredTargets: value });
              setDirty(true);
              setValidation(null);
            }}
            isReadOnly={readOnly}
          />
          <div style={{ display: "flex", gap: "0.4rem", flexWrap: "wrap" }}>
            {requiredTargets
              .split(",")
              .map((entry) => entry.trim())
              .filter(Boolean)
              .map((target) => (
                <Badge key={target} tone={producedTargets.has(target) ? "success" : "critical"}>
                  {`${target}: ${producedTargets.has(target) ? "mapped" : "NOT mapped"}`}
                </Badge>
              ))}
          </div>
        </section>

        <section aria-label="Field mappings" style={{ display: "grid", gap: "var(--soa-space-2)" }}>
          <h2 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>Field mappings</h2>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}>
            {fields.map((spec, index) => (
              <FieldRow
                key={index}
                spec={spec}
                index={index}
                scope="fields"
                errors={errorsFor("fields", index)}
                readOnly={readOnly}
                onChange={(next) =>
                  edit({
                    ...effectiveDefinition,
                    fields: fields.map((entry, i) => (i === index ? next : entry)),
                  })
                }
                onRemove={() =>
                  edit({
                    ...effectiveDefinition,
                    fields: fields.filter((_, i) => i !== index),
                  })
                }
              />
            ))}
          </ul>
          {!readOnly ? (
            <div>
              <Button
                size="sm"
                onPress={() =>
                  edit({
                    ...effectiveDefinition,
                    fields: [...fields, { target: "", source: "" }],
                  })
                }
              >
                Add field mapping
              </Button>
            </div>
          ) : null}
        </section>

        <section aria-label="Constants" style={{ display: "grid", gap: "var(--soa-space-2)" }}>
          <h2 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>Constants</h2>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}>
            {constants.map((spec, index) => (
              <li
                key={index}
                style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "end" }}
              >
                <TextField
                  label="Target field"
                  value={spec.target}
                  onChange={(value) =>
                    edit({
                      ...effectiveDefinition,
                      constants: constants.map((entry, i) =>
                        i === index ? { ...entry, target: value } : entry,
                      ),
                    })
                  }
                  isReadOnly={readOnly}
                />
                <TextField
                  label="Value"
                  value={String(spec.value ?? "")}
                  onChange={(value) =>
                    edit({
                      ...effectiveDefinition,
                      constants: constants.map((entry, i) =>
                        i === index ? { ...entry, value } : entry,
                      ),
                    })
                  }
                  isReadOnly={readOnly}
                />
                {!readOnly ? (
                  <Button
                    size="sm"
                    variant="subtle"
                    onPress={() =>
                      edit({
                        ...effectiveDefinition,
                        constants: constants.filter((_, i) => i !== index),
                      })
                    }
                  >
                    {`Remove constant ${index + 1}`}
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
          {!readOnly ? (
            <div>
              <Button
                size="sm"
                onPress={() =>
                  edit({
                    ...effectiveDefinition,
                    constants: [...constants, { target: "", value: "" }],
                  })
                }
              >
                Add constant
              </Button>
            </div>
          ) : null}
        </section>

        <section aria-label="Line items" style={{ display: "grid", gap: "var(--soa-space-2)" }}>
          <h2 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>Line items</h2>
          {effectiveDefinition.lines ? (
            <>
              <TextField
                label="Target array field"
                value={effectiveDefinition.lines.target}
                onChange={(value) =>
                  edit({
                    ...effectiveDefinition,
                    lines: { ...effectiveDefinition.lines!, target: value },
                  })
                }
                isReadOnly={readOnly}
              />
              <ul
                style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}
              >
                {lineFields.map((spec, index) => (
                  <FieldRow
                    key={index}
                    spec={spec}
                    index={index}
                    scope="lines"
                    errors={errorsFor("lines", index)}
                    readOnly={readOnly}
                    onChange={(next) =>
                      edit({
                        ...effectiveDefinition,
                        lines: {
                          ...effectiveDefinition.lines!,
                          fields: lineFields.map((entry, i) => (i === index ? next : entry)),
                        },
                      })
                    }
                    onRemove={() =>
                      edit({
                        ...effectiveDefinition,
                        lines: {
                          ...effectiveDefinition.lines!,
                          fields: lineFields.filter((_, i) => i !== index),
                        },
                      })
                    }
                  />
                ))}
              </ul>
              {!readOnly ? (
                <div style={{ display: "flex", gap: "var(--soa-space-2)" }}>
                  <Button
                    size="sm"
                    onPress={() =>
                      edit({
                        ...effectiveDefinition,
                        lines: {
                          ...effectiveDefinition.lines!,
                          fields: [...lineFields, { target: "", source: "" }],
                        },
                      })
                    }
                  >
                    Add line mapping
                  </Button>
                  <Button
                    size="sm"
                    variant="subtle"
                    onPress={() => edit({ ...effectiveDefinition, lines: undefined })}
                  >
                    Remove line section
                  </Button>
                </div>
              ) : null}
            </>
          ) : !readOnly ? (
            <div>
              <Button
                size="sm"
                onPress={() =>
                  edit({
                    ...effectiveDefinition,
                    lines: { source: "line_items", target: "Lines", fields: [] },
                  })
                }
              >
                Map line items
              </Button>
            </div>
          ) : (
            <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>No line mapping.</p>
          )}
        </section>

        {validation ? (
          <section
            aria-label="Sample preview"
            style={{ display: "grid", gap: "var(--soa-space-2)" }}
          >
            <h2 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>Sample preview</h2>
            {rowErrors.general.map((error) => (
              <Banner key={error} tone="critical" title="Mapping problem">
                {error}
              </Banner>
            ))}
            {validation.valid && validation.payload ? (
              <pre
                data-testid="mapping-preview"
                style={{
                  margin: 0,
                  overflowX: "auto",
                  font: "var(--soa-font-mono, monospace)",
                  fontSize: "0.8rem",
                }}
              >
                {JSON.stringify(validation.payload, null, 2)}
              </pre>
            ) : null}
          </section>
        ) : null}
      </div>
    </AppShell>
  );
}
