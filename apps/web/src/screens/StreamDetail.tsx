/**
 * Stream detail (CFG-010, UI_UX_BLUEPRINT §6.2).
 *
 * Hierarchy stays visible everywhere: breadcrumbs carry org → streams →
 * stream, and the parent process is named in the overview panel. Intake
 * endpoints and health render as honest placeholders until ING/ANA land.
 * Archiving is destructive-adjacent (stops intake), so it demands an
 * impact explanation that goes to the audit trail.
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
  archiveStream,
  fetchStreamDetail,
  fetchStreams,
  type StreamVersionSummary,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

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

function ArchiveDialog({ onConfirm }: { onConfirm: (impact: string) => void }) {
  const [impact, setImpact] = useState("");
  return (
    <Dialog title="Archive this stream?" alert>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0, font: "var(--soa-font-body-md)" }}>
            Archiving stops document intake for this stream. Existing documents and history are
            preserved. Explain the impact — it is recorded in the audit log.
          </p>
          <TextField
            label="Impact of archiving (required)"
            value={impact}
            onChange={setImpact}
            isRequired
          />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Keep the stream
            </Button>
            <Button
              variant="destructive"
              isDisabled={impact.trim().length < 10}
              onPress={() => {
                onConfirm(impact.trim());
                close();
              }}
            >
              Archive stream
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function VersionRow({ version }: { version: StreamVersionSummary }) {
  return (
    <li
      style={{
        display: "flex",
        gap: "var(--soa-space-3)",
        alignItems: "center",
        padding: "var(--soa-space-2) 0",
      }}
    >
      <strong>v{version.version_number}</strong>
      <Badge
        tone={
          version.state === "published" ? "success" : version.state === "draft" ? "info" : "neutral"
        }
      >
        {version.state}
      </Badge>
      {version.pinned_process_version_id ? (
        <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-secondary)" }}>
          pins process v{version.resolved_snapshot?.process_version_number}
        </span>
      ) : null}
    </li>
  );
}

export function StreamDetail() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("streams.manage");
  const { streamSlug } = useParams({ strict: false }) as { streamSlug: string };
  const queryClient = useQueryClient();

  const detail = useQuery({
    queryKey: ["stream", slug, streamSlug],
    queryFn: () => fetchStreamDetail(slug, streamSlug),
  });
  // Parent-process context for the hierarchy line.
  const streams = useQuery({ queryKey: ["streams", slug], queryFn: () => fetchStreams(slug) });

  const archive = useMutation({
    mutationFn: (impact: string) => archiveStream(slug, streamSlug, impact),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: ["stream", slug, streamSlug] });
      void queryClient.invalidateQueries({ queryKey: ["streams", slug] });
    },
  });

  if (detail.status === "pending") {
    return (
      <AppShell title="Stream" breadcrumbs={[{ label: session.organization.name }]}>
        <Skeleton height="12rem" />
      </AppShell>
    );
  }
  if (detail.status === "error") {
    return (
      <AppShell title="Stream" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load this stream"
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

  const { stream, versions } = detail.data;
  const parent = streams.data?.find((candidate) => candidate.slug === streamSlug);
  const published = versions.find((version) => version.state === "published");
  const config = published?.resolved_snapshot?.config ?? null;

  return (
    <AppShell
      title={stream.name}
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Streams", to: "/app/$organizationSlug/streams" },
        { label: stream.name },
      ]}
      actions={
        <div style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "center" }}>
          <Link
            to="/app/$organizationSlug/streams/$streamSlug/configure"
            params={{ organizationSlug: session.organization.slug, streamSlug }}
          >
            Edit configuration
          </Link>
          {canManage && stream.status !== "archived" ? (
            <DialogTrigger>
              <Button variant="destructive">Archive stream</Button>
              <ArchiveDialog onConfirm={(impact) => archive.mutate(impact)} />
            </DialogTrigger>
          ) : undefined}
        </div>
      }
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "56rem" }}>
        {archive.isError ? (
          <Banner tone="critical" title="Archive failed">
            {archive.error?.message ?? "The stream was not changed."}
          </Banner>
        ) : null}

        <Panel title="Overview">
          <dl
            style={{ display: "grid", gridTemplateColumns: "12rem 1fr", gap: "0.5rem", margin: 0 }}
          >
            <dt>Status</dt>
            <dd style={{ margin: 0 }}>
              <Badge tone={stream.status === "active" ? "success" : "neutral"}>
                {stream.status}
              </Badge>
            </dd>
            <dt>Process</dt>
            <dd style={{ margin: 0 }}>{parent ? parent.process_name : "…"}</dd>
            <dt>Intake endpoints</dt>
            <dd style={{ margin: 0 }}>
              <Badge tone="neutral">not configured yet — arrives with intake (ING)</Badge>
            </dd>
            <dt>Health</dt>
            <dd style={{ margin: 0 }}>
              <Badge tone="neutral">not tracked yet</Badge>
            </dd>
          </dl>
        </Panel>

        <Panel title="Published configuration">
          {config ? (
            <dl
              style={{
                display: "grid",
                gridTemplateColumns: "12rem 1fr",
                gap: "0.5rem",
                margin: 0,
              }}
            >
              {Object.entries(config).map(([key, value]) => (
                <div key={key} style={{ display: "contents" }}>
                  <dt style={{ fontFamily: "var(--soa-font-mono, monospace)" }}>{key}</dt>
                  <dd style={{ margin: 0 }}>{String(value)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <Banner tone="info" title="No published configuration">
              This stream has drafts but nothing published; documents cannot flow until a version is
              published.
            </Banner>
          )}
        </Panel>

        <Panel title="Changes">
          {versions.length === 0 ? (
            <p style={{ margin: 0, color: "var(--soa-text-secondary)" }}>No versions yet.</p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {[...versions].reverse().map((version) => (
                <VersionRow key={version.id} version={version} />
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </AppShell>
  );
}
