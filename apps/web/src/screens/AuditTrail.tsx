/**
 * Audit trail (ANA-007): the filtered audit query and the signed
 * export bundle. Every export shows its integrity manifest hash and
 * per-file SHA-256s next to the short-lived download links — an export
 * whose integrity cannot be verified is not worth handing out.
 */

import { Badge, Banner, Button, Skeleton, TextField } from "@soa/design-system";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { createAuditExport, fetchAuditEvents, type AuditExportResult } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

export function AuditTrail() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const [actionFilter, setActionFilter] = useState("");
  const [applied, setApplied] = useState("");
  const [exportResult, setExportResult] = useState<AuditExportResult | null>(null);

  const events = useQuery({
    queryKey: ["audit-events", slug, applied],
    queryFn: () => fetchAuditEvents(slug, { action: applied || undefined }),
    // Refresh on focus only — no interval, so rows never shift under an
    // investigator mid-read.
    refetchOnWindowFocus: true,
  });
  const exportBundle = useMutation({
    mutationFn: () => createAuditExport(slug, { action: applied || undefined }),
    onSuccess: setExportResult,
  });

  return (
    <AppShell
      title="Audit trail"
      breadcrumbs={[{ label: session.organization.name }, { label: "Audit trail" }]}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
        <div
          style={{
            display: "flex",
            gap: "var(--soa-space-2)",
            alignItems: "end",
            flexWrap: "wrap",
          }}
        >
          <TextField
            label="Filter by action prefix"
            value={actionFilter}
            onChange={setActionFilter}
          />
          <Button size="sm" variant="secondary" onPress={() => setApplied(actionFilter.trim())}>
            Apply filter
          </Button>
          <Button
            size="sm"
            isDisabled={exportBundle.isPending}
            onPress={() => exportBundle.mutate()}
          >
            Export signed bundle
          </Button>
        </div>

        {exportBundle.isError ? (
          <Banner tone="critical" title="Export failed">
            {exportBundle.error instanceof Error
              ? exportBundle.error.message
              : "The export did not complete."}
          </Banner>
        ) : null}
        {exportResult ? (
          <section
            aria-label="Export bundle"
            style={{
              border: "1px solid var(--soa-border)",
              borderRadius: "var(--soa-radius-panel)",
              padding: "var(--soa-space-3)",
              display: "grid",
              gap: "0.375rem",
            }}
          >
            <strong>
              Export {exportResult.export_id.slice(0, 8)} — {exportResult.event_count} event(s)
            </strong>
            <p style={{ margin: 0, font: "var(--soa-font-caption)" }}>
              Manifest SHA-256:{" "}
              <code style={{ overflowWrap: "anywhere" }}>{exportResult.manifest_sha256}</code> ·{" "}
              <a href={exportResult.manifest_download_url}>Download manifest</a> (link expires{" "}
              {exportResult.manifest_expires_at})
            </p>
            <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
              {exportResult.files.map((file) => (
                <li key={file.name} style={{ font: "var(--soa-font-caption)" }}>
                  <a href={file.download_url}>{file.name}</a> — {file.events} event(s), {file.bytes}{" "}
                  bytes, SHA-256 <code>{file.sha256}</code>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {events.status === "pending" ? <Skeleton height="16rem" /> : null}
        {events.status === "error" ? (
          <Banner tone="critical" title="Couldn’t load the audit trail">
            The audit service did not respond.
          </Banner>
        ) : null}
        {events.status === "success" ? (
          <section aria-label="Audit events" style={{ overflowX: "auto" }}>
            {events.data.items.length === 0 ? (
              <p style={{ margin: 0 }}>No audit events match this filter.</p>
            ) : (
              <table style={{ borderCollapse: "collapse", minWidth: "42rem" }}>
                <thead>
                  <tr>
                    <th style={{ textAlign: "left" }}>When (UTC)</th>
                    <th style={{ textAlign: "left" }}>Action</th>
                    <th style={{ textAlign: "left" }}>Actor</th>
                    <th style={{ textAlign: "left" }}>Target</th>
                  </tr>
                </thead>
                <tbody>
                  {events.data.items.map((event) => (
                    <tr key={event.id}>
                      <td>{event.occurred_at.slice(0, 19).replace("T", " ")}</td>
                      <td>
                        <Badge tone="neutral">{event.action}</Badge>
                      </td>
                      <td>{event.actor_id}</td>
                      <td>
                        {event.target_type}: {event.target_id}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {events.data.has_more ? (
              <p style={{ margin: 0, font: "var(--soa-font-caption)" }}>
                More events exist — narrow the filter or use the export bundle.
              </p>
            ) : null}
          </section>
        ) : null}
      </div>
    </AppShell>
  );
}
