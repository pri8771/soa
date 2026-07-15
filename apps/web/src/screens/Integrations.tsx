/**
 * Integrations list (EXP-004): every outbound destination with its
 * type, state, credential status, and a link into the mapping studio.
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
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { createIntegration, fetchIntegrations } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

export function Integrations() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const queryClient = useQueryClient();
  const integrations = useQuery({
    queryKey: ["integrations", slug],
    queryFn: () => fetchIntegrations(slug),
  });
  const [name, setName] = useState("");
  const [integrationSlug, setIntegrationSlug] = useState("");
  const [integrationType, setIntegrationType] = useState("webhook");
  const [endpointUrl, setEndpointUrl] = useState("");
  const create = useMutation({
    mutationFn: () =>
      createIntegration(slug, {
        name: name.trim(),
        slug: integrationSlug.trim(),
        integration_type: integrationType,
        endpoint_url: endpointUrl.trim() || null,
      }),
    onSuccess: () => {
      setName("");
      setIntegrationSlug("");
      setEndpointUrl("");
      void queryClient.invalidateQueries({ queryKey: ["integrations", slug] });
    },
  });
  const canManage = session.permissions.has("integrations.manage");

  return (
    <AppShell
      title="Integrations"
      breadcrumbs={[{ label: session.organization.name }, { label: "Integrations" }]}
      actions={
        canManage ? (
          <DialogTrigger>
            <Button>Create integration</Button>
            <Dialog title="Create integration">
              {({ close }) => (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    create.mutate(undefined, { onSuccess: close });
                  }}
                  style={{
                    display: "grid",
                    gap: "var(--soa-space-4)",
                    minWidth: "min(30rem, 80vw)",
                  }}
                >
                  <TextField label="Name" value={name} onChange={setName} isRequired />
                  <TextField
                    label="Slug"
                    description="Lowercase letters, numbers, and hyphens."
                    value={integrationSlug}
                    onChange={(value) => setIntegrationSlug(value.toLowerCase())}
                    isRequired
                  />
                  <label style={{ display: "grid", gap: "var(--soa-space-1)" }}>
                    Integration type
                    <select
                      value={integrationType}
                      onChange={(event) => setIntegrationType(event.target.value)}
                    >
                      <option value="webhook">Webhook</option>
                      <option value="quickbooks_online">QuickBooks Online</option>
                      <option value="netsuite">NetSuite (connection test only)</option>
                      <option value="microsoft_dynamics365">
                        Microsoft Dynamics 365 (connection test only)
                      </option>
                      <option value="sap_s4hana">SAP S/4HANA (connection test only)</option>
                    </select>
                  </label>
                  <TextField
                    label="Endpoint URL"
                    description="Configuration only; credentials are stored separately after creation."
                    type="url"
                    value={endpointUrl}
                    onChange={setEndpointUrl}
                  />
                  {create.isError ? (
                    <Banner tone="critical" title="Integration wasn’t created">
                      {create.error.message}
                    </Banner>
                  ) : null}
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "flex-end",
                      gap: "var(--soa-space-2)",
                    }}
                  >
                    <Button variant="subtle" onPress={close}>
                      Cancel
                    </Button>
                    <Button
                      type="submit"
                      variant="primary"
                      isDisabled={
                        !name.trim() ||
                        !/^[a-z0-9][a-z0-9-]*$/.test(integrationSlug) ||
                        create.isPending
                      }
                    >
                      Create
                    </Button>
                  </div>
                </form>
              )}
            </Dialog>
          </DialogTrigger>
        ) : undefined
      }
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
          Create a destination, configure its write-only credential, then publish a mapping before
          sending production orders.
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
              <Badge tone={integration.production_ready ? "success" : "critical"}>
                {integration.production_ready ? "delivery ready" : "delivery disabled"}
              </Badge>
              <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                {integration.endpoint_url ?? "no endpoint yet"}
              </span>
              {!integration.production_ready ? (
                <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                  {integration.readiness_detail}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </AppShell>
  );
}
