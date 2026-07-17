/**
 * Training workspace for a stream (extraction-training Phase 1).
 *
 * Lists the stream's training sets and creates new ones. A training set is a
 * stream-scoped gold dataset whose labelled samples become both few-shot
 * exemplars and the held-out evaluation slice. Deliberately plain — the UI is
 * being redesigned in parallel, so this favours clear, swappable structure
 * over styling.
 */

import { Badge, Banner, Button } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useState } from "react";

import { ApiError, createTrainingSet, fetchTrainingSets } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 100);
}

export function Training() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("streams.manage");
  const { streamSlug } = useParams({ strict: false }) as { streamSlug: string };
  const queryClient = useQueryClient();

  const sets = useQuery({
    queryKey: ["training-sets", slug, streamSlug],
    queryFn: () => fetchTrainingSets(slug, streamSlug),
  });

  const [name, setName] = useState("");
  const [slugValue, setSlugValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const slugTouched = slugValue.length > 0;
  const effectiveSlug = slugTouched ? slugValue : slugify(name);

  const create = useMutation({
    mutationFn: () =>
      createTrainingSet(slug, streamSlug, { name: name.trim(), slug: effectiveSlug }),
    onSuccess: () => {
      setName("");
      setSlugValue("");
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["training-sets", slug, streamSlug] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Could not create the set."),
  });

  const items = sets.data?.items ?? [];

  return (
    <AppShell
      title="Training"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Streams" },
        { label: streamSlug },
        { label: "Training" },
      ]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "56rem" }}>
        <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
          Upload sample documents, label their fields, and publish the set to teach this stream's
          extraction and measure the accuracy lift.
        </p>

        {!canManage ? (
          <Banner tone="warning" title="Read-only">
            Creating and labelling training sets needs the streams.manage permission.
          </Banner>
        ) : null}

        {canManage ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (name.trim() && effectiveSlug) create.mutate();
            }}
            style={{
              display: "flex",
              gap: "var(--soa-space-3)",
              alignItems: "flex-end",
              flexWrap: "wrap",
              padding: "var(--soa-space-4)",
              border: "1px solid var(--soa-border)",
              borderRadius: "var(--soa-radius-panel)",
            }}
          >
            <label style={{ display: "grid", gap: "var(--soa-space-1)", flex: "1 1 16rem" }}>
              <span style={{ font: "var(--soa-font-caption)" }}>Name</span>
              <input
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="e.g. UK purchase orders"
                style={inputStyle}
              />
            </label>
            <label style={{ display: "grid", gap: "var(--soa-space-1)", flex: "1 1 12rem" }}>
              <span style={{ font: "var(--soa-font-caption)" }}>Slug</span>
              <input
                value={effectiveSlug}
                onChange={(event) => setSlugValue(slugify(event.target.value))}
                placeholder="uk-purchase-orders"
                style={inputStyle}
              />
            </label>
            <Button type="submit" isDisabled={!name.trim() || !effectiveSlug || create.isPending}>
              Create training set
            </Button>
          </form>
        ) : null}

        {error ? (
          <Banner tone="critical" title="Couldn’t create the set">
            {error}
          </Banner>
        ) : null}

        {sets.status === "error" ? (
          <Banner tone="critical" title="Couldn’t load training sets">
            Try again.
          </Banner>
        ) : null}

        {items.length === 0 ? (
          <p style={{ color: "var(--soa-text-muted)" }}>No training sets yet.</p>
        ) : (
          <ul
            style={{
              listStyle: "none",
              margin: 0,
              padding: 0,
              display: "grid",
              gap: "var(--soa-space-2)",
            }}
          >
            {items.map((set) => (
              <li
                key={set.id}
                style={{
                  display: "flex",
                  gap: "var(--soa-space-3)",
                  alignItems: "center",
                  padding: "var(--soa-space-3)",
                  border: "1px solid var(--soa-border)",
                  borderRadius: "var(--soa-radius-control)",
                }}
              >
                <Link
                  to="/app/$organizationSlug/streams/$streamSlug/training/$trainingSlug"
                  params={{ organizationSlug: slug, streamSlug, trainingSlug: set.slug }}
                  style={{ fontWeight: 600 }}
                >
                  {set.name}
                </Link>
                <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                  {set.document_count} sample{set.document_count === 1 ? "" : "s"}
                </span>
                {set.published_version_id ? (
                  <Badge tone="success">Published</Badge>
                ) : (
                  <Badge tone="neutral">Draft</Badge>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </AppShell>
  );
}

const inputStyle: React.CSSProperties = {
  padding: "var(--soa-space-2)",
  border: "1px solid var(--soa-border)",
  borderRadius: "var(--soa-radius-control)",
  font: "inherit",
  background: "var(--soa-surface)",
  color: "inherit",
};
