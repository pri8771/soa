/**
 * Development pipeline console (dev builds only).
 *
 * A minimizable panel pinned to the bottom of the app that polls the job
 * queue and recent documents so you can see — live — what the worker is
 * doing at each step: which documents are processing, what state they are
 * in, and whether the worker is actually running. It is the answer to "I
 * uploaded a document, is anything happening?".
 *
 * Never rendered in a production build (guarded by import.meta.env.DEV at
 * the mount site).
 */

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { fetchDocuments, fetchJobStats } from "../../api/client";

const STORAGE_KEY = "soa.dev.console.collapsed";

const STATE_COLORS: Record<string, string> = {
  approved: "#3fb950",
  completed: "#3fb950",
  review_required: "#d29922",
  processing: "#58a6ff",
  queued: "#58a6ff",
  received: "#8b949e",
  rejected: "#f85149",
  quarantined: "#f85149",
  failed_terminal: "#f85149",
  failed_retryable: "#f85149",
};

function chip(label: string, value: number, color: string) {
  return (
    <span key={label} style={{ marginRight: 12, color: value > 0 ? color : "#6e7681" }}>
      {label} <strong>{value}</strong>
    </span>
  );
}

export function DevConsole({ organizationSlug }: { organizationSlug: string }) {
  const [collapsed, setCollapsed] = useState(
    () => globalThis.localStorage?.getItem(STORAGE_KEY) === "1",
  );

  const stats = useQuery({
    queryKey: ["dev", "jobStats", organizationSlug],
    queryFn: () => fetchJobStats(organizationSlug),
    refetchInterval: 2000,
  });
  const docs = useQuery({
    queryKey: ["dev", "documents", organizationSlug],
    queryFn: () => fetchDocuments(organizationSlug),
    refetchInterval: 2000,
    enabled: !collapsed,
  });

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    globalThis.localStorage?.setItem(STORAGE_KEY, next ? "1" : "0");
  };

  const byStatus = stats.data?.by_status ?? {};
  const pending = byStatus["pending"] ?? 0;
  const running = byStatus["running"] ?? 0;
  const succeeded = byStatus["succeeded"] ?? 0;
  const dead = byStatus["dead_letter"] ?? 0;
  const workerIdleWarning = pending > 0 && running === 0;

  return (
    <div
      style={{
        position: "fixed",
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: 9999,
        background: "#0d1117",
        color: "#c9d1d9",
        borderTop: "1px solid #30363d",
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 12,
        boxShadow: "0 -4px 16px rgba(0,0,0,0.3)",
      }}
      aria-label="Developer pipeline console"
    >
      <button
        type="button"
        onClick={toggle}
        style={{
          all: "unset",
          boxSizing: "border-box",
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: 8,
          width: "100%",
          padding: "6px 12px",
        }}
        aria-expanded={!collapsed}
      >
        <span style={{ fontWeight: 700 }}>⚙ Pipeline</span>
        <span>{collapsed ? "▸" : "▾"}</span>
        <span style={{ marginLeft: 8 }}>
          {chip("pending", pending, "#58a6ff")}
          {chip("running", running, "#d29922")}
          {chip("done", succeeded, "#3fb950")}
          {chip("dead", dead, "#f85149")}
        </span>
        {stats.isError && <span style={{ color: "#f85149" }}>· API unreachable</span>}
        {workerIdleWarning && (
          <span style={{ color: "#d29922", marginLeft: "auto" }}>
            ⚠ {pending} pending, worker idle — is `uv run soa-worker` running?
          </span>
        )}
      </button>

      {!collapsed && (
        <div
          style={{
            maxHeight: 200,
            overflowY: "auto",
            padding: "8px 12px",
            borderTop: "1px solid #21262d",
          }}
        >
          <div style={{ color: "#8b949e", marginBottom: 6 }}>
            recent documents (updates every 2s)
          </div>
          {(docs.data?.items ?? []).length === 0 ? (
            <div style={{ color: "#6e7681" }}>
              no documents yet — upload one from Documents → Upload
            </div>
          ) : (
            (docs.data?.items ?? []).slice(0, 12).map((d) => (
              <div key={d.id} style={{ display: "flex", gap: 10, padding: "2px 0" }}>
                <span style={{ color: "#8b949e", minWidth: 160 }}>
                  {new Date(d.received_at).toLocaleTimeString()}
                </span>
                <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {d.original_filename}
                </span>
                <span style={{ color: STATE_COLORS[d.state] ?? "#c9d1d9", minWidth: 130 }}>
                  {d.state}
                </span>
                {d.state_reason && (
                  <span style={{ color: "#6e7681", flex: 2, overflow: "hidden" }}>
                    {d.state_reason}
                  </span>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
