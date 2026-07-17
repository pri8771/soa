/**
 * Annotation view for one training sample (extraction-training Phase 1).
 *
 * Left: the document with the drawing surface (reuses DocumentViewer's
 * draw/assign). Right: the stream's fields (header + line-item columns). Select
 * a field, draw a box on the page, and the enclosed text becomes the expected
 * value (editable) while the box is saved as a positional hint. Saving writes
 * the whole ground truth to the training set's draft as one labelled sample.
 */

import { Badge, Banner, Button, Select } from "@soa/design-system";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import {
  ApiError,
  documentTextInRegion,
  fetchSchema,
  fetchStreams,
  fetchTrainingSet,
  upsertTrainingDocument,
  type SchemaField,
  type TrainingGroundTruth,
  type TrainingRegion,
} from "../api/client";
import { DocumentViewer, type EvidenceHighlight } from "../components/viewer/DocumentViewer";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

type Split = "train" | "validation" | "test";
type Region = TrainingRegion;

interface FieldState {
  value: string;
  region: Region | null;
}

const SPLITS: Split[] = ["train", "validation", "test"];

// A line-cell target is keyed lines.<rowIndex>.<column>; header targets are the
// bare field key. Both label the same region map.
const lineTarget = (row: number, col: string) => `lines.${row}.${col}`;

export function TrainingAnnotate() {
  const session = useShellSession();
  const org = session.organization.slug;
  const { streamSlug, trainingSlug, documentId } = useParams({ strict: false }) as {
    streamSlug: string;
    trainingSlug: string;
    documentId: string;
  };

  const streams = useQuery({
    queryKey: ["streams", org],
    queryFn: () => fetchStreams(org),
  });
  const processSlug = streams.data?.find((s) => s.slug === streamSlug)?.process_slug;
  const schema = useQuery({
    queryKey: ["schema", org, processSlug],
    queryFn: () => fetchSchema(org, processSlug as string),
    enabled: Boolean(processSlug),
  });
  const trainingSet = useQuery({
    queryKey: ["training-set", org, streamSlug, trainingSlug],
    queryFn: () => fetchTrainingSet(org, streamSlug, trainingSlug),
  });

  const published = schema.data?.versions.find((v) => v.state === "published");
  const headerFields = useMemo<SchemaField[]>(
    () => (published?.definition.fields ?? []).filter((f) => f.type !== "table"),
    [published],
  );
  const tableField = useMemo<SchemaField | undefined>(
    () => (published?.definition.fields ?? []).find((f) => f.type === "table"),
    [published],
  );
  const lineColumns = useMemo<SchemaField[]>(() => tableField?.columns ?? [], [tableField]);

  const existing = trainingSet.data?.documents.find((d) => d.source_document_id === documentId);

  // --- annotation state (initialised once fields + existing labels load) ---
  const [ready, setReady] = useState(false);
  const [split, setSplit] = useState<Split>("train");
  const [header, setHeader] = useState<Record<string, FieldState>>({});
  const [rows, setRows] = useState<Record<string, FieldState>[]>([]);
  const [activeTarget, setActiveTarget] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  // Seed state from schema + any existing labels, exactly once. Gated on the
  // schema having ANY labellable field — header OR line-item — so a table-only
  // (line-item-only) schema still seeds and never silently drops saved labels.
  if (!ready && (headerFields.length > 0 || lineColumns.length > 0) && !trainingSet.isLoading) {
    const gt = existing?.ground_truth;
    const regions = gt?.regions ?? {};
    const seedHeader: Record<string, FieldState> = {};
    for (const field of headerFields) {
      seedHeader[field.key] = {
        value: gt?.fields?.[field.key] ?? "",
        region: regions[field.key] ?? null,
      };
    }
    const seedRows: Record<string, FieldState>[] = (gt?.lines ?? []).map((line, index) => {
      const row: Record<string, FieldState> = {};
      for (const col of lineColumns) {
        row[col.key] = {
          value: line[col.key] ?? "",
          region: regions[lineTarget(index, col.key)] ?? null,
        };
      }
      return row;
    });
    setSplit((existing?.split as Split) ?? "train");
    setHeader(seedHeader);
    setRows(seedRows);
    setReady(true);
  }

  const setTargetValue = (target: string, value: string) => {
    if (target.startsWith("lines.")) {
      const [, rowStr, col] = target.split(".");
      const rowIndex = Number(rowStr);
      setRows((current) =>
        current.map((row, index) =>
          index === rowIndex ? { ...row, [col]: { ...row[col], value } } : row,
        ),
      );
    } else {
      setHeader((current) => ({ ...current, [target]: { ...current[target], value } }));
    }
  };
  const setTargetRegion = (target: string, region: Region | null) => {
    if (target.startsWith("lines.")) {
      const [, rowStr, col] = target.split(".");
      const rowIndex = Number(rowStr);
      setRows((current) =>
        current.map((row, index) =>
          index === rowIndex ? { ...row, [col]: { ...row[col], region } } : row,
        ),
      );
    } else {
      setHeader((current) => ({ ...current, [target]: { ...current[target], region } }));
    }
  };

  const onRegionDrawn = async (region: { pageNumber: number; polygon: number[][] }) => {
    if (!activeTarget) return;
    const stored: Region = { page_number: region.pageNumber, polygon: region.polygon };
    setTargetRegion(activeTarget, stored);
    try {
      const found = await documentTextInRegion(org, documentId, {
        page_number: region.pageNumber,
        polygon: region.polygon,
      });
      if (found.text) setTargetValue(activeTarget, found.text);
    } catch {
      // Text lookup is a convenience; the box is already stored and the user
      // can type the value by hand.
    }
  };

  const evidence = useMemo<EvidenceHighlight[]>(() => {
    const items: EvidenceHighlight[] = [];
    for (const [key, state] of Object.entries(header)) {
      if (state.region) {
        items.push({
          id: key,
          label: key,
          page_number: state.region.page_number,
          polygon: state.region.polygon as [number, number][],
          kind: key === activeTarget ? "active" : "related",
        });
      }
    }
    rows.forEach((row, index) => {
      for (const col of lineColumns) {
        const state = row[col.key];
        if (state?.region) {
          const target = lineTarget(index, col.key);
          items.push({
            id: target,
            label: target,
            page_number: state.region.page_number,
            polygon: state.region.polygon as [number, number][],
            kind: target === activeTarget ? "active" : "related",
          });
        }
      }
    });
    return items;
  }, [header, rows, lineColumns, activeTarget]);

  const buildGroundTruth = (): TrainingGroundTruth => {
    const fields: Record<string, string | null> = {};
    const regions: Record<string, Region> = {};
    for (const field of headerFields) {
      const state = header[field.key];
      if (!state) continue;
      const value = state.value.trim();
      if (value) fields[field.key] = value;
      else if (state.region) fields[field.key] = null;
      if (state.region) regions[field.key] = state.region;
    }
    // Build the compacted lines and their region keys together so a line-cell
    // region is keyed by the row's FINAL (compacted) index — dropping empty
    // rows must not leave region keys pointing at shifted or absent lines.
    const lines: Record<string, string | null>[] = [];
    for (const row of rows) {
      const line: Record<string, string | null> = {};
      let hasValue = false;
      for (const col of lineColumns) {
        const value = row[col.key]?.value.trim() ?? "";
        line[col.key] = value || null;
        if (value) hasValue = true;
      }
      if (!hasValue) continue; // drop empty rows — and, with them, their regions
      const compactIndex = lines.length;
      lines.push(line);
      for (const col of lineColumns) {
        const region = row[col.key]?.region;
        if (region) regions[lineTarget(compactIndex, col.key)] = region;
      }
    }
    const gt: TrainingGroundTruth = { fields, validations: [] };
    if (lines.length > 0) gt.lines = lines;
    if (Object.keys(regions).length > 0) gt.regions = regions;
    return gt;
  };

  const labelledCount =
    Object.values(header).filter((s) => s.value.trim() || s.region).length +
    rows.reduce(
      (acc, row) => acc + Object.values(row).filter((s) => s?.value.trim() || s?.region).length,
      0,
    );

  const save = useMutation({
    mutationFn: () =>
      upsertTrainingDocument(org, streamSlug, trainingSlug, {
        source_document_id: documentId,
        split,
        ground_truth: buildGroundTruth(),
      }),
    onSuccess: () => {
      setError(null);
      setSavedAt(new Date().toLocaleTimeString());
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : "Could not save these labels."),
  });

  const backLink = (
    <Link
      to="/app/$organizationSlug/streams/$streamSlug/training/$trainingSlug"
      params={{ organizationSlug: org, streamSlug, trainingSlug }}
    >
      Back to training set
    </Link>
  );

  if (schema.data && !published) {
    return (
      <AppShell title="Annotate sample" breadcrumbs={[{ label: trainingSlug }]} actions={backLink}>
        <Banner tone="warning" title="This stream has no published schema">
          Publish a schema for the stream before labelling — the field list comes from it.
        </Banner>
      </AppShell>
    );
  }

  return (
    <AppShell
      title="Label sample"
      breadcrumbs={[
        { label: session.organization.name },
        { label: streamSlug },
        { label: trainingSlug },
        { label: "Label" },
      ]}
      actions={backLink}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "minmax(0, 1.2fr) minmax(22rem, 1fr)",
          gap: "var(--soa-space-4)",
          alignItems: "start",
        }}
      >
        <div
          style={{
            minHeight: "70vh",
            border: "1px solid var(--soa-border)",
            borderRadius: "var(--soa-radius-panel)",
            overflow: "hidden",
          }}
        >
          <DocumentViewer
            organizationSlug={org}
            documentId={documentId}
            evidence={evidence}
            activeEvidenceId={activeTarget}
            drawTarget={activeTarget}
            onEvidenceSelect={(id) => setActiveTarget(id)}
            onRegionDrawn={(region) => void onRegionDrawn(region)}
          />
        </div>

        <div style={{ display: "grid", gap: "var(--soa-space-4)" }}>
          <div
            style={{
              display: "flex",
              gap: "var(--soa-space-3)",
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            <div style={{ maxWidth: "12rem" }}>
              <Select
                label="Split"
                items={SPLITS.map((s) => ({ id: s, label: s }))}
                selectedKey={split}
                onSelectionChange={(key) => setSplit(String(key) as Split)}
              />
            </div>
            <Button
              onPress={() => save.mutate()}
              isDisabled={labelledCount === 0 || save.isPending}
            >
              Save labels
            </Button>
            {savedAt ? <Badge tone="success">Saved {savedAt}</Badge> : null}
          </div>

          <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
            Select a field, then draw a box on the document — the enclosed text fills the value.
          </p>

          {error ? (
            <Banner tone="critical" title="Save failed">
              {error}
            </Banner>
          ) : null}

          <section style={{ display: "grid", gap: "var(--soa-space-2)" }}>
            <h3 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>Fields</h3>
            {headerFields.map((field) => (
              <FieldRow
                key={field.key}
                label={field.label || field.key}
                critical={field.criticality === "critical"}
                active={activeTarget === field.key}
                state={header[field.key] ?? { value: "", region: null }}
                onSelect={() => setActiveTarget(field.key)}
                onValue={(value) => setTargetValue(field.key, value)}
                onClear={() => setTargetRegion(field.key, null)}
              />
            ))}
          </section>

          {lineColumns.length > 0 ? (
            <section style={{ display: "grid", gap: "var(--soa-space-2)" }}>
              <div
                style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}
              >
                <h3 style={{ margin: 0, font: "var(--soa-font-heading-sm)" }}>
                  Line items ({rows.length})
                </h3>
                <Button
                  size="sm"
                  variant="subtle"
                  onPress={() =>
                    setRows((current) => [
                      ...current,
                      Object.fromEntries(
                        lineColumns.map((c) => [c.key, { value: "", region: null }]),
                      ),
                    ])
                  }
                >
                  Add row
                </Button>
              </div>
              {rows.map((row, index) => (
                <div
                  key={index}
                  style={{
                    display: "grid",
                    gap: "var(--soa-space-1)",
                    padding: "var(--soa-space-2)",
                    border: "1px solid var(--soa-border)",
                    borderRadius: "var(--soa-radius-control)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <span style={{ font: "var(--soa-font-caption)" }}>Row {index + 1}</span>
                    <Button
                      size="sm"
                      variant="subtle"
                      onPress={() => setRows((current) => current.filter((_, i) => i !== index))}
                    >
                      Remove
                    </Button>
                  </div>
                  {lineColumns.map((col) => {
                    const target = lineTarget(index, col.key);
                    return (
                      <FieldRow
                        key={col.key}
                        label={col.label || col.key}
                        active={activeTarget === target}
                        state={row[col.key] ?? { value: "", region: null }}
                        onSelect={() => setActiveTarget(target)}
                        onValue={(value) => setTargetValue(target, value)}
                        onClear={() => setTargetRegion(target, null)}
                      />
                    );
                  })}
                </div>
              ))}
            </section>
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}

function FieldRow({
  label,
  critical,
  active,
  state,
  onSelect,
  onValue,
  onClear,
}: {
  label: string;
  critical?: boolean;
  active: boolean;
  state: FieldState;
  onSelect: () => void;
  onValue: (value: string) => void;
  onClear: () => void;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "auto 1fr auto",
        gap: "var(--soa-space-2)",
        alignItems: "center",
      }}
    >
      <button
        type="button"
        onClick={onSelect}
        aria-pressed={active}
        title="Select, then draw its box"
        style={{
          cursor: "pointer",
          border: `1px solid var(${active ? "--soa-accent" : "--soa-border"})`,
          background: active ? "var(--soa-accent-muted, var(--soa-surface))" : "var(--soa-surface)",
          borderRadius: "var(--soa-radius-control)",
          padding: "var(--soa-space-1) var(--soa-space-2)",
          font: "var(--soa-font-caption)",
          color: "inherit",
          whiteSpace: "nowrap",
        }}
      >
        {label}
        {critical ? " *" : ""}
      </button>
      <input
        value={state.value}
        onChange={(event) => onValue(event.target.value)}
        placeholder="value"
        style={{
          padding: "var(--soa-space-1) var(--soa-space-2)",
          border: "1px solid var(--soa-border)",
          borderRadius: "var(--soa-radius-control)",
          font: "inherit",
          background: "var(--soa-surface)",
          color: "inherit",
          minWidth: 0,
        }}
      />
      {state.region ? (
        <button
          type="button"
          onClick={onClear}
          title="Clear the box"
          style={{
            cursor: "pointer",
            border: "none",
            background: "none",
            color: "var(--soa-text-muted)",
          }}
        >
          📍 clear
        </button>
      ) : (
        <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>—</span>
      )}
    </div>
  );
}
