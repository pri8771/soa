/**
 * Canonical payload viewer (CAN-004).
 *
 * Three views over the approved canonical order: a FORMATTED business
 * summary, a collapsible TREE, and the raw JSON. Copy and download are
 * permission-gated (the server says so via `can_copy`), and a redacted
 * payload announces itself — redaction happened server-side, so the
 * withheld content was never sent here. Large payloads stay safe: the
 * tree caps children per node and the JSON view truncates its preview,
 * pointing at download for the full document.
 */

import { Badge, Banner, Button } from "@soa/design-system";
import { useState } from "react";

import type { CanonicalPayloadResponse } from "../../api/client";
import type { LineItem, Money } from "../../api/canonical-order";

type ViewMode = "formatted" | "tree" | "json";

//: Tree nodes render at most this many children; the JSON preview stops
//: at this many characters. Everything is always in the DOWNLOAD.
const MAX_TREE_CHILDREN = 100;
const MAX_JSON_PREVIEW = 20_000;

function money(value: Money | null | undefined): string {
  return value ? `${value.amount} ${value.currency}` : "—";
}

function TreeNode({ label, value, depth }: { label: string; value: unknown; depth: number }) {
  if (value === null || typeof value !== "object") {
    return (
      <li>
        <span style={{ color: "var(--soa-text-muted)" }}>{label}:</span> {JSON.stringify(value)}
      </li>
    );
  }
  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item] as const)
    : Object.entries(value as Record<string, unknown>);
  const shown = entries.slice(0, MAX_TREE_CHILDREN);
  return (
    <li>
      <details open={depth === 0}>
        <summary>
          {label} {Array.isArray(value) ? `(${entries.length})` : ""}
        </summary>
        <ul style={{ listStyle: "none", paddingInlineStart: "1rem", margin: 0 }}>
          {shown.map(([key, child]) => (
            <TreeNode key={key} label={key} value={child} depth={depth + 1} />
          ))}
          {entries.length > MAX_TREE_CHILDREN ? (
            <li style={{ color: "var(--soa-text-muted)" }}>
              … {entries.length - MAX_TREE_CHILDREN} more — download the JSON for everything.
            </li>
          ) : null}
        </ul>
      </details>
    </li>
  );
}

export function PayloadViewer({ data }: { data: CanonicalPayloadResponse }) {
  const [view, setView] = useState<ViewMode>("formatted");
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const payload = data.payload;
  const json = JSON.stringify(payload, null, 2);

  const copyDownloadReason = data.can_copy
    ? null
    : "Copying and downloading the payload needs the documents.review permission.";

  const download = () => {
    const blob = new Blob([json], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `canonical-order-${payload.identifiers.po_number}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    setAnnouncement("Payload downloaded.");
  };

  return (
    <div style={{ display: "grid", gap: "var(--soa-space-3)" }} data-testid="payload-viewer">
      <div style={{ display: "flex", gap: "var(--soa-space-2)", flexWrap: "wrap" }}>
        <span
          style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}
          data-testid="payload-meta"
        >
          schema {data.schema_version} · sha256 {data.sha256.slice(0, 12)}…
          {data.created_by ? ` · approved by ${data.created_by}` : ""}
        </span>
        {data.redacted ? <Badge tone="warning">redacted view</Badge> : null}
      </div>
      {data.redacted ? (
        <Banner tone="info" title="Redacted view">
          Internal notes and reviewer identities are withheld for read-only access — they were
          removed by the server, not hidden here.
        </Banner>
      ) : null}

      <div role="group" aria-label="Payload view" style={{ display: "flex", gap: "0.25rem" }}>
        {(["formatted", "tree", "json"] as const).map((mode) => (
          <Button
            key={mode}
            size="sm"
            variant={view === mode ? "primary" : "secondary"}
            aria-pressed={view === mode}
            onPress={() => setView(mode)}
          >
            {mode === "formatted" ? "Formatted" : mode === "tree" ? "Tree" : "JSON"}
          </Button>
        ))}
        <span style={{ flex: 1 }} />
        <Button
          size="sm"
          isDisabled={!data.can_copy}
          onPress={() => {
            void navigator.clipboard.writeText(json).then(
              () => setAnnouncement("Payload copied to the clipboard."),
              () => setAnnouncement("Copy failed — the clipboard is unavailable."),
            );
          }}
        >
          Copy JSON
        </Button>
        <Button size="sm" isDisabled={!data.can_copy} onPress={download}>
          Download
        </Button>
      </div>
      {copyDownloadReason ? (
        <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
          {copyDownloadReason}
        </p>
      ) : null}
      <div role="status" aria-live="polite">
        {announcement ? <p style={{ margin: 0 }}>{announcement}</p> : null}
      </div>

      {view === "formatted" ? (
        <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "12rem minmax(0, 1fr)",
              gap: "0.4rem",
              margin: 0,
            }}
          >
            <dt>PO number</dt>
            <dd style={{ margin: 0 }}>{payload.identifiers.po_number}</dd>
            <dt>Order date</dt>
            <dd style={{ margin: 0 }}>{payload.dates.order_date}</dd>
            <dt>Currency</dt>
            <dd style={{ margin: 0 }}>{payload.terms.currency}</dd>
            <dt>Buyer</dt>
            <dd style={{ margin: 0 }}>{payload.parties?.buyer?.name ?? "—"}</dd>
            <dt>Grand total</dt>
            <dd style={{ margin: 0 }}>{money(payload.totals.grand_total)}</dd>
          </dl>
          <div style={{ overflowX: "auto" }}>
            <table style={{ borderCollapse: "collapse" }}>
              <caption
                style={{
                  textAlign: "left",
                  font: "var(--soa-font-caption)",
                  paddingBottom: "0.3rem",
                }}
              >
                Line items ({payload.line_items.length})
              </caption>
              <thead>
                <tr>
                  {["#", "SKU", "Description", "Qty", "Unit price", "Line total"].map((h) => (
                    <th key={h} style={{ textAlign: "left", padding: "0.2rem 0.75rem 0.2rem 0" }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {payload.line_items.slice(0, MAX_TREE_CHILDREN).map((item: LineItem) => (
                  <tr key={item.line_number}>
                    <td style={{ padding: "0.2rem 0.75rem 0.2rem 0" }}>{item.line_number}</td>
                    <td style={{ padding: "0.2rem 0.75rem 0.2rem 0" }}>{item.sku ?? "—"}</td>
                    <td style={{ padding: "0.2rem 0.75rem 0.2rem 0" }}>
                      {item.description ?? "—"}
                    </td>
                    <td style={{ padding: "0.2rem 0.75rem 0.2rem 0" }}>{item.quantity}</td>
                    <td style={{ padding: "0.2rem 0.75rem 0.2rem 0" }}>{money(item.unit_price)}</td>
                    <td style={{ padding: "0.2rem 0.75rem 0.2rem 0" }}>{money(item.line_total)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {payload.line_items.length > MAX_TREE_CHILDREN ? (
            <p style={{ margin: 0, font: "var(--soa-font-caption)" }}>
              Showing the first {MAX_TREE_CHILDREN} of {payload.line_items.length} lines — download
              the JSON for the full order.
            </p>
          ) : null}
        </div>
      ) : null}

      {view === "tree" ? (
        <ul
          style={{ listStyle: "none", paddingInlineStart: 0, margin: 0 }}
          data-testid="payload-tree"
        >
          {Object.entries(payload).map(([key, value]) => (
            <TreeNode key={key} label={key} value={value} depth={1} />
          ))}
        </ul>
      ) : null}

      {view === "json" ? (
        <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
          {json.length > MAX_JSON_PREVIEW ? (
            <Banner tone="info" title="Preview truncated">
              The payload is {json.length.toLocaleString()} characters; the preview shows the first{" "}
              {MAX_JSON_PREVIEW.toLocaleString()}. Download the JSON for the full document.
            </Banner>
          ) : null}
          <pre
            style={{
              margin: 0,
              overflowX: "auto",
              font: "var(--soa-font-mono, monospace)",
              fontSize: "0.8rem",
            }}
            data-testid="payload-json"
          >
            {json.slice(0, MAX_JSON_PREVIEW)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}
