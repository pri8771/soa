/**
 * Version timeline, structured diff, and rollback (CFG-014,
 * UI_UX_BLUEPRINT §6.7).
 *
 * The timeline lists every version with its state and change summary —
 * history is append-only and rollback never rewrites it, it publishes the
 * old definition again through the audited service (CFG-007). The diff is
 * structural (per-key added/removed/changed), never a raw JSON dump, and
 * values under secret-looking keys are redacted client-side as defense in
 * depth — configuration must reference credentials (TEN-009), never embed
 * them, so anything that trips the pattern is already a smell.
 */

import {
  Badge,
  Banner,
  Button,
  Dialog,
  DialogTrigger,
  Select,
  TextField,
} from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "@tanstack/react-router";
import { useState } from "react";

import {
  fetchProcessDetail,
  fetchProcessVersion,
  rollbackProcess,
  type ProcessVersionSummary,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const SECRET_KEY_PATTERN = /secret|token|password|credential|api[_-]?key|private[_-]?key/i;

export function displayValue(key: string, value: unknown): string {
  if (SECRET_KEY_PATTERN.test(key)) return "•••••• (redacted)";
  return value === undefined ? "—" : JSON.stringify(value);
}

export interface DiffRow {
  key: string;
  kind: "added" | "removed" | "changed";
  from: unknown;
  to: unknown;
}

export function structuredDiff(
  from: Record<string, unknown>,
  to: Record<string, unknown>,
): DiffRow[] {
  const keys = [...new Set([...Object.keys(from), ...Object.keys(to)])].sort();
  const rows: DiffRow[] = [];
  for (const key of keys) {
    const inFrom = key in from;
    const inTo = key in to;
    if (inFrom && !inTo) rows.push({ key, kind: "removed", from: from[key], to: undefined });
    else if (!inFrom && inTo) rows.push({ key, kind: "added", from: undefined, to: to[key] });
    else if (JSON.stringify(from[key]) !== JSON.stringify(to[key]))
      rows.push({ key, kind: "changed", from: from[key], to: to[key] });
  }
  return rows;
}

function RollbackDialog({
  versionNumber,
  onConfirm,
}: {
  versionNumber: number;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  return (
    <Dialog title={`Roll back to v${versionNumber}?`} alert>
      {({ close }) => (
        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <p style={{ margin: 0, font: "var(--soa-font-body-md)" }}>
            Rolling back republishes v{versionNumber} as the active configuration. History is
            preserved — nothing is deleted — and the reason is recorded in the audit log.
          </p>
          <TextField
            label="Reason for rolling back (required)"
            value={reason}
            onChange={setReason}
            isRequired
          />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", justifyContent: "flex-end" }}>
            <Button variant="subtle" onPress={close}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              isDisabled={reason.trim().length < 3}
              onPress={() => {
                onConfirm(reason.trim());
                close();
              }}
            >
              Roll back
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

const KIND_TONE = { added: "success", removed: "critical", changed: "warning" } as const;

export function ProcessVersions() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canManage = session.permissions.has("processes.manage");
  const { processSlug } = useParams({ strict: false }) as { processSlug: string };
  const queryClient = useQueryClient();

  const detail = useQuery({
    queryKey: ["process", slug, processSlug],
    queryFn: () => fetchProcessDetail(slug, processSlug),
  });

  const versions: ProcessVersionSummary[] = detail.data?.versions ?? [];
  const newestFirst = [...versions].sort((a, b) => b.version_number - a.version_number);
  const activeId = detail.data?.process.active_version_id ?? null;

  // Default comparison: the active version against the one published before it.
  const [fromId, setFromId] = useState<string | null>(null);
  const [toId, setToId] = useState<string | null>(null);
  const activeIndex = newestFirst.findIndex((v) => v.id === activeId);
  const defaultTo = activeId ?? newestFirst[0]?.id ?? null;
  const defaultFrom =
    activeIndex >= 0 && activeIndex + 1 < newestFirst.length
      ? newestFirst[activeIndex + 1].id
      : null;
  const compareFrom = fromId ?? defaultFrom;
  const compareTo = toId ?? defaultTo;

  const fromVersion = useQuery({
    queryKey: ["process-version", slug, processSlug, compareFrom],
    queryFn: () => fetchProcessVersion(slug, processSlug, compareFrom as string),
    enabled: compareFrom !== null,
  });
  const toVersion = useQuery({
    queryKey: ["process-version", slug, processSlug, compareTo],
    queryFn: () => fetchProcessVersion(slug, processSlug, compareTo as string),
    enabled: compareTo !== null,
  });

  const rollback = useMutation({
    mutationFn: ({ versionId, reason }: { versionId: string; reason: string }) =>
      rollbackProcess(slug, processSlug, versionId, reason),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["process", slug, processSlug] });
    },
  });

  if (detail.status === "error") {
    return (
      <AppShell title="Versions" breadcrumbs={[{ label: session.organization.name }]}>
        <Banner
          tone="critical"
          title="Couldn’t load this process"
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

  const versionItems = newestFirst.map((v) => ({ id: v.id, label: `v${v.version_number}` }));
  const diffRows =
    fromVersion.data && toVersion.data
      ? structuredDiff(fromVersion.data.definition, toVersion.data.definition)
      : null;

  return (
    <AppShell
      title="Version history"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Processes", to: "/app/$organizationSlug/processes" },
        { label: detail.data?.process.name ?? processSlug },
        { label: "Versions" },
      ]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
        {rollback.isError ? (
          <Banner tone="critical" title="Rollback refused">
            {rollback.error?.message ?? "The active version was not changed."}
          </Banner>
        ) : null}

        <section aria-label="Version timeline">
          <h2 style={{ font: "var(--soa-font-heading-md)" }}>Timeline</h2>
          <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {newestFirst.map((version) => (
              <li
                key={version.id}
                style={{
                  display: "flex",
                  gap: "var(--soa-space-3)",
                  alignItems: "center",
                  flexWrap: "wrap",
                  padding: "var(--soa-space-3) 0",
                  borderBottom: "1px solid var(--soa-border)",
                }}
              >
                <strong>v{version.version_number}</strong>
                <Badge
                  tone={
                    version.state === "published"
                      ? "success"
                      : version.state === "draft"
                        ? "info"
                        : "neutral"
                  }
                >
                  {version.id === activeId ? "active" : version.state}
                </Badge>
                <span style={{ font: "var(--soa-font-caption)" }}>
                  {version.change_summary ?? "no change summary"}
                </span>
                {version.published_at ? (
                  <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                    published {new Date(version.published_at).toLocaleString()} by{" "}
                    {version.published_by ?? "unknown"}
                  </span>
                ) : null}
                {canManage && version.state === "superseded" ? (
                  <DialogTrigger>
                    <Button size="sm" variant="subtle">
                      Roll back to v{version.version_number}
                    </Button>
                    <RollbackDialog
                      versionNumber={version.version_number}
                      onConfirm={(reason) => rollback.mutate({ versionId: version.id, reason })}
                    />
                  </DialogTrigger>
                ) : null}
              </li>
            ))}
          </ol>
          {newestFirst.length === 0 ? (
            <p style={{ color: "var(--soa-text-muted)" }}>
              No versions yet. Create a draft from the process to begin.
            </p>
          ) : null}
        </section>

        <section
          aria-label="Compare versions"
          style={{ display: "grid", gap: "var(--soa-space-3)" }}
        >
          <h2 style={{ font: "var(--soa-font-heading-md)", margin: 0 }}>Compare</h2>
          <div style={{ display: "flex", gap: "var(--soa-space-3)", maxWidth: "24rem" }}>
            <Select
              label="From version"
              items={versionItems}
              selectedKey={compareFrom}
              onSelectionChange={(key) => setFromId(String(key))}
            />
            <Select
              label="To version"
              items={versionItems}
              selectedKey={compareTo}
              onSelectionChange={(key) => setToId(String(key))}
            />
          </div>
          {diffRows === null ? (
            <p style={{ color: "var(--soa-text-muted)" }}>
              {versions.length < 2
                ? "Comparison needs at least two versions."
                : "Select two versions to compare."}
            </p>
          ) : diffRows.length === 0 ? (
            <p style={{ color: "var(--soa-text-muted)" }}>
              The two versions have identical configuration.
            </p>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ borderCollapse: "collapse", textAlign: "left" }}>
                <caption style={{ position: "absolute", clip: "rect(0 0 0 0)" }}>
                  Configuration differences
                </caption>
                <thead>
                  <tr>
                    <th style={{ padding: "var(--soa-space-2)" }}>Setting</th>
                    <th style={{ padding: "var(--soa-space-2)" }}>Change</th>
                    <th style={{ padding: "var(--soa-space-2)" }}>From</th>
                    <th style={{ padding: "var(--soa-space-2)" }}>To</th>
                  </tr>
                </thead>
                <tbody>
                  {diffRows.map((row) => (
                    <tr key={row.key} style={{ borderTop: "1px solid var(--soa-border)" }}>
                      <td style={{ padding: "var(--soa-space-2)" }}>
                        <code>{row.key}</code>
                      </td>
                      <td style={{ padding: "var(--soa-space-2)" }}>
                        <Badge tone={KIND_TONE[row.kind]}>{row.kind}</Badge>
                      </td>
                      <td style={{ padding: "var(--soa-space-2)", overflowWrap: "anywhere" }}>
                        {row.kind === "added" ? "—" : displayValue(row.key, row.from)}
                      </td>
                      <td style={{ padding: "var(--soa-space-2)", overflowWrap: "anywhere" }}>
                        {row.kind === "removed" ? "—" : displayValue(row.key, row.to)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </AppShell>
  );
}
