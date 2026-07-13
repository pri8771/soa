/**
 * Stream inheritance editor (CFG-013, UI_UX_BLUEPRINT §6.6).
 *
 * Every setting is labeled with where its value comes from — platform
 * default, inherited from the parent process, or overridden on this
 * stream — using the resolver's provenance (CFG-006), never guessed
 * client-side. Overrides are edited on a draft version, reset-to-parent
 * removes the override so the inherited value shows through immediately,
 * and the resolved preview (with fingerprint) is recomputed server-side
 * on every change so what you see is exactly what publish would pin.
 */

import { Badge, Banner, Button, TextField } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";

import {
  createStreamVersion,
  fetchStreamDetail,
  publishStreamVersion,
  resolveStreamPreview,
  updateStreamVersion,
  type ResolvePreview,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

/** Text inputs come back as strings; overrides should keep the value's
 * natural JSON type so the resolver compares like with like. */
function coerce(raw: string): unknown {
  const trimmed = raw.trim();
  if (trimmed === "true") return true;
  if (trimmed === "false") return false;
  if (trimmed !== "" && !Number.isNaN(Number(trimmed))) return Number(trimmed);
  return raw;
}

function sourceBadge(source: "environment" | "process" | "stream") {
  if (source === "stream") return <Badge tone="warning">Overridden on this stream</Badge>;
  if (source === "process") return <Badge tone="info">Inherited from process</Badge>;
  return <Badge tone="neutral">Platform default</Badge>;
}

export function StreamConfigure() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("streams.manage");
  const { streamSlug } = useParams({ strict: false }) as { streamSlug: string };
  const queryClient = useQueryClient();

  const detail = useQuery({
    queryKey: ["stream", slug, streamSlug],
    queryFn: () => fetchStreamDetail(slug, streamSlug),
  });

  const versions = detail.data?.versions ?? [];
  const draft = [...versions].reverse().find((v) => v.state === "draft");
  const published = [...versions].reverse().find((v) => v.state === "published");
  const baseline = useMemo(
    () => draft?.overrides ?? published?.overrides ?? {},
    [draft, published],
  );

  const [overrides, setOverrides] = useState<Record<string, unknown>>(baseline);
  useEffect(() => setOverrides(baseline), [baseline]);
  const dirty = JSON.stringify(overrides) !== JSON.stringify(baseline);

  // Draft rows for a setting being added: named but not yet configured.
  const [pendingKey, setPendingKey] = useState("");
  const [pendingValue, setPendingValue] = useState("");

  const resolve = useQuery<ResolvePreview>({
    queryKey: ["resolve", slug, streamSlug, JSON.stringify(overrides)],
    queryFn: () => resolveStreamPreview(slug, streamSlug, overrides),
    enabled: detail.status === "success",
    retry: false,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["stream", slug, streamSlug] });
  };
  const save = useMutation({
    mutationFn: () =>
      draft
        ? updateStreamVersion(slug, streamSlug, draft.id, draft.version, overrides)
        : createStreamVersion(slug, streamSlug, overrides),
    onSuccess: invalidate,
  });
  const publish = useMutation({
    mutationFn: (versionId: string) => publishStreamVersion(slug, streamSlug, versionId),
    onSuccess: invalidate,
  });

  const layers = resolve.data?.layers;
  const rowKeys = useMemo(() => {
    const keys = new Set<string>([
      ...Object.keys(layers?.environment ?? {}),
      ...Object.keys(layers?.process ?? {}),
      ...Object.keys(overrides),
    ]);
    return [...keys].sort();
  }, [layers, overrides]);

  const inheritedValue = (key: string): unknown =>
    layers?.process && key in layers.process
      ? layers.process[key]
      : (layers?.environment ?? {})[key];

  const sourceOf = (key: string): "environment" | "process" | "stream" => {
    if (key in overrides) return "stream";
    if (layers?.process && key in layers.process) return "process";
    return "environment";
  };

  if (detail.status === "error") {
    return (
      <AppShell title="Configuration" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load this stream"
          action={
            <Button size="sm" onPress={() => void detail.refetch()}>
              Try again
            </Button>
          }
        >
          Nothing has been changed.
        </Banner>
      </AppShell>
    );
  }

  const streamName = detail.data?.stream.name ?? streamSlug;

  return (
    <AppShell
      title="Stream configuration"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Streams", to: "/app/$organizationSlug/streams" },
        { label: streamName },
        { label: "Configuration" },
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
        <Banner tone="info" title="Scope of these changes">
          Overrides here affect only the “{streamName}” stream. Other streams of the same process
          keep their own configuration, and nothing takes effect until the draft is published.
        </Banner>

        {resolve.status === "error" ? (
          <Banner tone="warning" title="Resolved preview unavailable">
            {resolve.error?.message ??
              "The parent process has no published version to resolve against."}{" "}
            You can still edit overrides; the preview appears once the process publishes.
          </Banner>
        ) : null}
        {save.isError ? (
          <Banner tone="critical" title="The configuration was not saved">
            {save.error?.message ?? "Saving failed."}
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

        <section aria-label="Settings" style={{ display: "grid", gap: "var(--soa-space-3)" }}>
          {rowKeys.map((key) => {
            const source = sourceOf(key);
            const effective = key in overrides ? overrides[key] : inheritedValue(key);
            return (
              <div
                key={key}
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
                <TextField
                  label={key}
                  value={effective === null || effective === undefined ? "" : String(effective)}
                  isDisabled={!canManage}
                  onChange={(raw) =>
                    setOverrides((current) => ({ ...current, [key]: coerce(raw) }))
                  }
                />
                {sourceBadge(source)}
                {source === "stream" && canManage ? (
                  <Button
                    size="sm"
                    variant="subtle"
                    aria-label={`Reset ${key} to parent`}
                    onPress={() =>
                      setOverrides((current) => {
                        const next = { ...current };
                        delete next[key];
                        return next;
                      })
                    }
                  >
                    Reset to parent
                  </Button>
                ) : null}
              </div>
            );
          })}
        </section>

        {canManage ? (
          <section
            aria-label="Add setting"
            style={{
              display: "flex",
              gap: "var(--soa-space-3)",
              alignItems: "end",
              flexWrap: "wrap",
            }}
          >
            <TextField label="Setting key" value={pendingKey} onChange={setPendingKey} />
            <TextField label="Setting value" value={pendingValue} onChange={setPendingValue} />
            {pendingKey.trim() ? <Badge tone="neutral">Not configured</Badge> : null}
            <Button
              size="sm"
              isDisabled={!pendingKey.trim()}
              onPress={() => {
                setOverrides((current) => ({
                  ...current,
                  [pendingKey.trim()]: coerce(pendingValue),
                }));
                setPendingKey("");
                setPendingValue("");
              }}
            >
              Add override
            </Button>
          </section>
        ) : null}

        <section aria-label="Resolved configuration">
          <h2 style={{ font: "var(--soa-font-heading-md)" }}>Resolved preview</h2>
          {resolve.data ? (
            <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
              <dl
                style={{
                  display: "grid",
                  gridTemplateColumns: "auto auto auto",
                  gap: "var(--soa-space-2) var(--soa-space-5)",
                  justifyContent: "start",
                  margin: 0,
                }}
              >
                {Object.entries(resolve.data.resolved.values).map(([key, entry]) => (
                  <div key={key} style={{ display: "contents" }}>
                    <dt style={{ font: "var(--soa-font-body-strong)" }}>{key}</dt>
                    <dd style={{ margin: 0 }}>{JSON.stringify(entry.value)}</dd>
                    <dd style={{ margin: 0 }}>{sourceBadge(entry.source)}</dd>
                  </div>
                ))}
              </dl>
              <p style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                Resolves against process v{resolve.data.resolved.process_version_number} ·
                fingerprint <code>{resolve.data.resolved.fingerprint.slice(0, 12)}…</code>
              </p>
            </div>
          ) : (
            <p style={{ color: "var(--soa-text-muted)" }}>
              {resolve.status === "pending"
                ? "Resolving…"
                : "No resolved preview until the parent process has a published version."}
            </p>
          )}
        </section>
      </div>
    </AppShell>
  );
}
