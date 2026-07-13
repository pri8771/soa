/**
 * Integrations list (EXP-004): every outbound destination with its
 * type, state, credential status, and a link into the mapping studio.
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { fetchIntegrations } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

export function Integrations() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const integrations = useQuery({
    queryKey: ["integrations", slug],
    queryFn: () => fetchIntegrations(slug),
  });

  return (
    <AppShell
      title="Integrations"
      breadcrumbs={[{ label: session.organization.name }, { label: "Integrations" }]}
    >
      <p style={{ margin: "0 0 1rem" }}>
        <Link to="/app/$organizationSlug/providers" params={{ organizationSlug: slug }}>
          Provider catalog →
        </Link>
      </p>
      {integrations.status === "pending" ? <Skeleton height="10rem" /> : null}
      {integrations.status === "error" ? (
        <Banner
          tone="critical"
          title="Couldn’t load integrations"
          action={
            <Button size="sm" onPress={() => void integrations.refetch()}>
              Try again
            </Button>
          }
        >
          The service did not respond.
        </Banner>
      ) : null}
      {integrations.status === "success" && integrations.data.items.length === 0 ? (
        <Banner tone="info" title="No integrations yet">
          Integrations are created through the API today; the creation form arrives with the
          delivery milestone.
        </Banner>
      ) : null}
      {integrations.status === "success" && integrations.data.items.length > 0 ? (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.75rem" }}>
          {integrations.data.items.map((integration) => (
            <li
              key={integration.id}
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
              <Link
                to="/app/$organizationSlug/integrations/$integrationSlug"
                params={{ organizationSlug: slug, integrationSlug: integration.slug }}
                style={{ font: "var(--soa-font-heading-sm)" }}
              >
                {integration.name}
              </Link>
              <Badge tone="neutral">{integration.integration_type}</Badge>
              <Badge tone={integration.status === "active" ? "success" : "warning"}>
                {integration.status}
              </Badge>
              <Badge tone={integration.credential_configured ? "success" : "warning"}>
                {integration.credential_configured ? "credential set" : "no credential"}
              </Badge>
              <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                {integration.endpoint_url ?? "no endpoint yet"}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </AppShell>
  );
}
