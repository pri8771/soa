/**
 * Classifier builder — the routing table on an intake stream (docs/ROUTING.md).
 *
 * A route matches on signals (customer names, letterheads) found in a
 * document's own text; the most-distinct-hits route wins and ties or zero
 * hits fail closed to a human in the unrouted queue — this screen only
 * authors the table, the matcher itself is server-side and deterministic.
 * Same draft/publish lifecycle as rules/instructions/schema.
 */

import { Badge, Banner, Button, Select, TextField } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";

import {
  createClassifierDraft,
  fetchClassifier,
  fetchStreams,
  publishClassifierVersion,
  updateClassifierDraft,
  type ClassifierRoute,
  type ClassifierVersion,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

function newRoute(defaultTarget: string): ClassifierRoute {
  return { label: "", target_stream_id: defaultTarget, signals: [""] };
}

function RouteEditor({
  route,
  targets,
  onChange,
  onRemove,
}: {
  route: ClassifierRoute;
  targets: { id: string; label: string }[];
  onChange: (next: ClassifierRoute) => void;
  onRemove: () => void;
}) {
  return (
    <div
      style={{
        display: "grid",
        gap: "var(--soa-space-3)",
        padding: "var(--soa-space-4)",
        border: "1.5px solid var(--soa-border-strong)",
      }}
    >
      <div
        style={{ display: "flex", gap: "var(--soa-space-3)", alignItems: "end", flexWrap: "wrap" }}
      >
        <TextField
          label="Label"
          value={route.label}
          onChange={(label) => onChange({ ...route, label })}
        />
        <Select
          label="Routes to skill"
          items={targets}
          selectedKey={route.target_stream_id}
          onSelectionChange={(key) => onChange({ ...route, target_stream_id: String(key) })}
        />
        <Button
          size="sm"
          variant="destructive"
          aria-label={`Remove route ${route.label || "unnamed"}`}
          onPress={onRemove}
        >
          Remove route
        </Button>
      </div>

      <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
        <span
          style={{
            font: "700 9.5px/12px var(--soa-font-family)",
            letterSpacing: "0.12em",
            color: "var(--soa-text-muted)",
          }}
        >
          SIGNALS
        </span>
        {route.signals.map((signal, index) => (
          <div
            key={index}
            style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "end" }}
          >
            <TextField
              label={`Signal ${index + 1}`}
              value={signal}
              onChange={(next) =>
                onChange({
                  ...route,
                  signals: route.signals.map((s, i) => (i === index ? next : s)),
                })
              }
            />
            {route.signals.length > 1 ? (
              <Button
                size="sm"
                variant="subtle"
                aria-label={`Remove signal ${index + 1} from ${route.label || "unnamed"}`}
                onPress={() =>
                  onChange({ ...route, signals: route.signals.filter((_, i) => i !== index) })
                }
              >
                Remove
              </Button>
            ) : null}
          </div>
        ))}
        <div>
          <Button
            size="sm"
            variant="subtle"
            onPress={() => onChange({ ...route, signals: [...route.signals, ""] })}
          >
            Add signal
          </Button>
        </div>
      </div>
    </div>
  );
}

export function ClassifierBuilder() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("streams.manage");
  const { streamSlug } = useParams({ strict: false }) as { streamSlug: string };
  const queryClient = useQueryClient();

  const listing = useQuery({
    queryKey: ["classifier", slug, streamSlug],
    queryFn: () => fetchClassifier(slug, streamSlug),
  });
  const streams = useQuery({ queryKey: ["streams", slug], queryFn: () => fetchStreams(slug) });

  const versions: ClassifierVersion[] = listing.data?.items ?? [];
  const draft = versions.find((v) => v.state === "draft");
  const published = versions.find((v) => v.state === "published");
  const baseline = useMemo(
    () => draft?.content ?? published?.content ?? { routes: [] },
    [draft, published],
  );

  const targets = useMemo(
    () =>
      (streams.data ?? [])
        .filter((s) => s.slug !== streamSlug && s.status !== "archived")
        .map((s) => ({ id: s.id, label: `${s.name} (${s.process_name})` })),
    [streams.data, streamSlug],
  );

  const [routes, setRoutes] = useState<ClassifierRoute[]>(baseline.routes);
  useEffect(() => setRoutes(baseline.routes), [baseline]);
  const dirty = JSON.stringify(routes) !== JSON.stringify(baseline.routes);

  const invalidate = () =>
    void queryClient.invalidateQueries({ queryKey: ["classifier", slug, streamSlug] });
  const save = useMutation({
    mutationFn: () =>
      draft
        ? updateClassifierDraft(slug, draft.id, { routes })
        : createClassifierDraft(slug, streamSlug, { routes }),
    onSuccess: invalidate,
  });
  const publish = useMutation({
    mutationFn: (versionId: string) => publishClassifierVersion(slug, versionId),
    onSuccess: invalidate,
  });

  if (listing.status === "error") {
    return (
      <AppShell title="Routing" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load the routing table"
          action={
            <Button size="sm" onPress={() => void listing.refetch()}>
              Try again
            </Button>
          }
        >
          Nothing has been changed.
        </Banner>
      </AppShell>
    );
  }

  const noTargets = streams.data !== undefined && targets.length === 0;

  return (
    <AppShell
      title="Routing"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Streams", to: "/app/$organizationSlug/streams" },
        { label: streamSlug },
        { label: "Routing" },
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
      <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "64rem" }}>
        {noTargets ? (
          <Banner tone="warning" title="No other skills to route to">
            Routes need a target skill in this organization. Create another stream first.
          </Banner>
        ) : null}
        {save.isError ? (
          <Banner tone="critical" title="The routing table was not saved">
            {save.error?.message ?? "Validation failed."}
          </Banner>
        ) : null}
        {publish.isError ? (
          <Banner tone="critical" title="Publish refused">
            {publish.error?.message ?? "The draft did not validate."}
          </Banner>
        ) : null}
        {published && !draft && !dirty ? (
          <Banner tone="info" title={`Editing starts from published ${published.reference}`}>
            Your first change creates a new draft; the published version stays untouched and keeps
            routing documents until you publish again.
          </Banner>
        ) : null}
        {routes.length === 0 ? (
          <Banner tone="info" title="No routes yet">
            With no published classifier, every document processes directly on this stream — the
            same as a single-skill bucket. Add a route once this intake needs to split documents
            across skills.
          </Banner>
        ) : null}

        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          {routes.map((route, index) => (
            <RouteEditor
              key={index}
              route={route}
              targets={targets}
              onChange={(next) =>
                setRoutes((current) => current.map((r, i) => (i === index ? next : r)))
              }
              onRemove={() => setRoutes((current) => current.filter((_, i) => i !== index))}
            />
          ))}
          <div>
            <Button
              isDisabled={targets.length === 0}
              onPress={() => setRoutes((current) => [...current, newRoute(targets[0]?.id ?? "")])}
            >
              Add route
            </Button>
          </div>
        </div>
      </div>
    </AppShell>
  );
}
