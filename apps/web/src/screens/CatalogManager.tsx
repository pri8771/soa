/**
 * Catalog manager (CAT-005): versions with explicit activation, the
 * import wizard (file → column mapping → validation report → draft),
 * and the record browser. Partial failures are CLEAR: every row issue
 * renders with its row number in an accessible table, and a draft with
 * skipped rows says so before anyone can activate it.
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useParams } from "@tanstack/react-router";

import {
  activateCatalogVersion,
  fetchCatalogDetail,
  fetchCatalogRecords,
  importCatalogFile,
  type CatalogImportResult,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("could not read the file"));
    reader.onload = () => {
      const url = String(reader.result);
      resolve(url.slice(url.indexOf(",") + 1)); // strip the data: prefix
    };
    reader.readAsDataURL(file);
  });
}

function ImportWizard({
  organizationSlug,
  catalogSlug,
}: {
  organizationSlug: string;
  catalogSlug: string;
}) {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [mapping, setMapping] = useState({
    source_id: "sku",
    display_name: "name",
    aliases: "",
    effective_from: "",
  });
  const [sheet, setSheet] = useState("");
  const [allowPartial, setAllowPartial] = useState(false);
  const [result, setResult] = useState<CatalogImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useMutation({
    mutationFn: async (dryRun: boolean) => {
      if (!file) throw new Error("Choose a file first.");
      const content = await fileToBase64(file);
      return importCatalogFile(organizationSlug, catalogSlug, {
        filename: file.name,
        content_base64: content,
        mapping: {
          source_id: mapping.source_id,
          display_name: mapping.display_name,
          aliases: mapping.aliases || null,
          effective_from: mapping.effective_from || null,
        },
        sheet: sheet || null,
        allow_partial: allowPartial,
        dry_run: dryRun,
      });
    },
    onSuccess: (imported) => {
      setResult(imported);
      setError(null);
      if (imported.status === "draft_created") {
        void queryClient.invalidateQueries({
          queryKey: ["catalog", organizationSlug, catalogSlug],
        });
      }
    },
    onError: (mutationError: Error) => setError(mutationError.message),
  });

  const field = (label: string, key: keyof typeof mapping) => (
    <label style={{ display: "grid", gap: "0.15rem" }}>
      {label}
      <input
        value={mapping[key]}
        onChange={(event) => setMapping({ ...mapping, [key]: event.target.value })}
      />
    </label>
  );

  return (
    <section
      aria-label="Import records"
      style={{
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
        padding: "var(--soa-space-4)",
        display: "grid",
        gap: "0.75rem",
      }}
    >
      <h2 style={{ margin: 0, fontSize: "1rem" }}>Import records</h2>
      <label style={{ display: "grid", gap: "0.15rem" }}>
        File (.csv, .xlsx, .xlsm)
        <input
          type="file"
          accept=".csv,.xlsx,.xlsm"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
      </label>
      <fieldset
        style={{
          border: "1px solid var(--soa-border)",
          borderRadius: "var(--soa-radius-panel)",
          display: "grid",
          gap: "0.5rem",
          gridTemplateColumns: "repeat(auto-fit, minmax(11rem, 1fr))",
        }}
      >
        <legend>Column mapping</legend>
        {field("Source ID column", "source_id")}
        {field("Display name column", "display_name")}
        {field("Aliases column (optional)", "aliases")}
        {field("Effective-from column (optional)", "effective_from")}
        <label style={{ display: "grid", gap: "0.15rem" }}>
          Sheet (XLSX only, optional)
          <input value={sheet} onChange={(event) => setSheet(event.target.value)} />
        </label>
      </fieldset>
      <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap" }}>
        <Button size="sm" onPress={() => run.mutate(true)} isDisabled={!file || run.isPending}>
          Preview (dry run)
        </Button>
        <label>
          <input
            type="checkbox"
            checked={allowPartial}
            onChange={(event) => setAllowPartial(event.target.checked)}
          />{" "}
          Import valid rows even if some fail (explicit)
        </label>
        <Button size="sm" onPress={() => run.mutate(false)} isDisabled={!file || run.isPending}>
          Create draft version
        </Button>
      </div>
      {error ? (
        <Banner tone="critical" title="Import refused">
          {error}
        </Banner>
      ) : null}
      {result ? (
        <div style={{ display: "grid", gap: "0.5rem" }}>
          <p style={{ margin: 0 }}>
            <strong>{result.status === "previewed" ? "Preview" : "Draft created"}</strong> —{" "}
            {result.records} valid rows · {result.issues.length} failed rows · added{" "}
            {result.preview.added.length}, changed {result.preview.changed.length}, deactivated{" "}
            {result.preview.deactivated.length}, unchanged {result.preview.unchanged}
          </p>
          {result.issues.length > 0 ? (
            <table style={{ borderCollapse: "collapse" }}>
              <caption style={{ textAlign: "left", fontWeight: 600 }}>
                Failed rows — fix these in the file or import partially (explicit)
              </caption>
              <thead>
                <tr>
                  <th scope="col" style={{ textAlign: "left" }}>
                    Row
                  </th>
                  <th scope="col" style={{ textAlign: "left" }}>
                    Problem
                  </th>
                </tr>
              </thead>
              <tbody>
                {result.issues.map((issue) => (
                  <tr key={`${issue.row_number}:${issue.message}`}>
                    <td>{issue.row_number}</td>
                    <td>{issue.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {result.status === "draft_created" && result.version ? (
            <Banner tone="success" title={`Draft v${result.version.version_number} created`}>
              Review it below, then activate explicitly — imports never go live on their own.
            </Banner>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function RecordBrowser({
  organizationSlug,
  catalogSlug,
  versionId,
}: {
  organizationSlug: string;
  catalogSlug: string;
  versionId: string;
}) {
  const [query, setQuery] = useState("");
  const records = useQuery({
    queryKey: ["catalog-records", organizationSlug, catalogSlug, versionId, query],
    queryFn: () =>
      fetchCatalogRecords(organizationSlug, catalogSlug, versionId, { q: query || undefined }),
  });
  return (
    <section aria-label="Records" style={{ display: "grid", gap: "0.5rem" }}>
      <label style={{ display: "grid", gap: "0.15rem", maxWidth: "20rem" }}>
        Search records
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="source id, name, or alias"
        />
      </label>
      {records.status === "pending" ? <Skeleton height="6rem" /> : null}
      {records.status === "success" ? (
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <caption style={{ textAlign: "left", fontWeight: 600 }}>
            Records in the selected version
          </caption>
          <thead>
            <tr>
              <th scope="col" style={{ textAlign: "left" }}>
                Source ID
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Name
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Aliases
              </th>
              <th scope="col" style={{ textAlign: "left" }}>
                Effective
              </th>
            </tr>
          </thead>
          <tbody>
            {records.data.items.map((record) => (
              <tr key={record.id} style={{ borderTop: "1px solid var(--soa-border)" }}>
                <th scope="row" style={{ textAlign: "left", fontWeight: 500 }}>
                  {record.source_id}
                </th>
                <td>{record.display_name}</td>
                <td>{record.aliases.join(", ") || "—"}</td>
                <td>
                  {record.effective_from ?? "…"} → {record.effective_to ?? "…"}
                </td>
              </tr>
            ))}
            {records.data.items.length === 0 ? (
              <tr>
                <td colSpan={4}>No records match.</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      ) : null}
    </section>
  );
}

export function CatalogManager() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const { catalogSlug } = useParams({ strict: false }) as { catalogSlug: string };
  const queryClient = useQueryClient();
  const canActivate = session.permissions.has("catalogs.activate");
  const [browsedVersion, setBrowsedVersion] = useState<string | null>(null);

  const detail = useQuery({
    queryKey: ["catalog", slug, catalogSlug],
    queryFn: () => fetchCatalogDetail(slug, catalogSlug),
  });
  const activate = useMutation({
    mutationFn: (versionId: string) => activateCatalogVersion(slug, catalogSlug, versionId),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["catalog", slug, catalogSlug] }),
  });

  return (
    <AppShell
      title={detail.data?.catalog.name ?? "Catalog"}
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Catalogs", to: "/app/$organizationSlug/catalogs" },
        { label: catalogSlug },
      ]}
    >
      {detail.status === "pending" ? <Skeleton height="12rem" /> : null}
      {detail.status === "error" ? (
        <Banner tone="critical" title="Couldn’t load the catalog">
          The service did not respond.
        </Banner>
      ) : null}
      {detail.status === "success" ? (
        <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
          <section aria-label="Versions" style={{ display: "grid", gap: "0.5rem" }}>
            <h2 style={{ margin: 0, fontSize: "1rem" }}>Versions</h2>
            <ul
              style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}
            >
              {detail.data.versions.map((version) => (
                <li
                  key={version.id}
                  style={{
                    border: "1px solid var(--soa-border)",
                    borderRadius: "var(--soa-radius-panel)",
                    padding: "var(--soa-space-3)",
                    display: "flex",
                    gap: "0.75rem",
                    alignItems: "center",
                    flexWrap: "wrap",
                  }}
                >
                  <strong>v{version.version_number}</strong>
                  <Badge
                    tone={
                      version.state === "published"
                        ? "success"
                        : version.state === "draft"
                          ? "warning"
                          : "neutral"
                    }
                  >
                    {version.state === "published" ? "active" : version.state}
                  </Badge>
                  <span>{version.record_count} records</span>
                  {version.change_summary ? (
                    <span style={{ color: "var(--soa-text-muted)" }}>{version.change_summary}</span>
                  ) : null}
                  <span style={{ marginLeft: "auto", display: "flex", gap: "0.5rem" }}>
                    <Button
                      size="sm"
                      variant="subtle"
                      onPress={() => setBrowsedVersion(version.id)}
                    >
                      Browse records
                    </Button>
                    {version.state === "draft" && canActivate ? (
                      <Button
                        size="sm"
                        onPress={() => activate.mutate(version.id)}
                        isDisabled={activate.isPending}
                      >
                        Activate
                      </Button>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
            {activate.status === "error" ? (
              <Banner tone="critical" title="Activation refused">
                {(activate.error as Error).message}
              </Banner>
            ) : null}
            {activate.status === "success" ? (
              <Banner tone="success" title={`v${activate.data.version_number} is now active`}>
                Matching now runs against this version.
              </Banner>
            ) : null}
          </section>

          <ImportWizard organizationSlug={slug} catalogSlug={catalogSlug} />

          {browsedVersion ? (
            <RecordBrowser
              organizationSlug={slug}
              catalogSlug={catalogSlug}
              versionId={browsedVersion}
            />
          ) : null}
        </div>
      ) : null}
    </AppShell>
  );
}
