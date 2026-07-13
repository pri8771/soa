/**
 * Catalogs list (CAT-005): every reference-data catalog with its type,
 * source, and live-version status, linking into the manager.
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { fetchCatalogs } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

export function Catalogs() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const catalogs = useQuery({
    queryKey: ["catalogs", slug],
    queryFn: () => fetchCatalogs(slug),
  });

  return (
    <AppShell
      title="Catalogs"
      breadcrumbs={[{ label: session.organization.name }, { label: "Catalogs" }]}
    >
      {catalogs.status === "pending" ? <Skeleton height="10rem" /> : null}
      {catalogs.status === "error" ? (
        <Banner
          tone="critical"
          title="Couldn’t load catalogs"
          action={
            <Button size="sm" onPress={() => void catalogs.refetch()}>
              Try again
            </Button>
          }
        >
          The service did not respond.
        </Banner>
      ) : null}
      {catalogs.status === "success" && catalogs.data.items.length === 0 ? (
        <Banner tone="info" title="No catalogs yet">
          Create a catalog through the API, then import its records here.
        </Banner>
      ) : null}
      {catalogs.status === "success" && catalogs.data.items.length > 0 ? (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.75rem" }}>
          {catalogs.data.items.map((catalog) => (
            <li
              key={catalog.id}
              style={{
                border: "1px solid var(--soa-border)",
                borderRadius: "var(--soa-radius-panel)",
                padding: "var(--soa-space-4)",
                display: "flex",
                gap: "var(--soa-space-3)",
                alignItems: "center",
                flexWrap: "wrap",
              }}
            >
              <div style={{ flex: 1, minWidth: "12rem" }}>
                <Link
                  to="/app/$organizationSlug/catalogs/$catalogSlug"
                  params={{ organizationSlug: slug, catalogSlug: catalog.slug }}
                >
                  <strong>{catalog.name}</strong>
                </Link>
                <div style={{ color: "var(--soa-text-muted)", fontSize: "0.85rem" }}>
                  {catalog.slug}
                </div>
              </div>
              <Badge tone="neutral">{catalog.catalog_type}</Badge>
              <Badge tone="neutral">{catalog.source}</Badge>
              <Badge tone={catalog.active_version_id ? "success" : "warning"}>
                {catalog.active_version_id ? "active version live" : "no active version"}
              </Badge>
            </li>
          ))}
        </ul>
      ) : null}
    </AppShell>
  );
}
