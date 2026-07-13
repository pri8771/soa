/**
 * Provider administration (AIO-019): the shipped provider catalog with
 * capability, region, languages, health, data policy, and the tenant's
 * approval/credential reference — plus a routing preview. Data-policy
 * warnings are explicit and secrets are never displayed (only the
 * credential REFERENCE name exists on this surface).
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { fetchProviders, previewProviderRouting, type ProviderEntry } from "../api/client";
import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

const CAPABILITIES = ["native_text", "ocr", "classify", "split", "field_extraction"];

function ProviderCard({ provider }: { provider: ProviderEntry }) {
  const risky =
    provider.data_policy.sends_content_to_third_party ||
    provider.data_policy.retains_content ||
    provider.data_policy.uses_content_for_training;
  return (
    <li
      style={{
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-panel)",
        padding: "var(--soa-space-4)",
        display: "grid",
        gap: "0.5rem",
      }}
    >
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
        <strong>{provider.name}</strong>
        <Badge tone="neutral">{provider.capability}</Badge>
        <Badge tone={provider.local ? "success" : "warning"}>
          {provider.local ? "local" : `region: ${provider.region}`}
        </Badge>
        <Badge tone="neutral">{`health: ${provider.health}`}</Badge>
        {provider.approved ? <Badge tone="success">approved</Badge> : null}
      </div>
      <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>{provider.description}</p>
      <p style={{ margin: 0, fontSize: "0.85rem" }}>
        Languages: {provider.languages.join(", ")} · Availability: {provider.availability}
      </p>
      <ul style={{ margin: 0, paddingLeft: "1.2rem" }}>
        {provider.warnings.map((warning) => (
          <li key={warning} style={{ color: risky ? "var(--soa-critical, #b00)" : "inherit" }}>
            {warning}
          </li>
        ))}
      </ul>
      {provider.approved ? (
        <p style={{ margin: 0, fontSize: "0.85rem" }}>
          Credential reference: <code>{provider.credential_ref ?? "(none)"}</code> — the secret
          itself is never displayed anywhere.
        </p>
      ) : null}
    </li>
  );
}

export function Providers() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const providers = useQuery({
    queryKey: ["providers", slug],
    queryFn: () => fetchProviders(slug),
  });
  const [capability, setCapability] = useState("field_extraction");
  const [localOnly, setLocalOnly] = useState(false);
  const preview = useMutation({
    mutationFn: () => previewProviderRouting(slug, { capability, local_only: localOnly }),
  });

  return (
    <AppShell
      title="Providers"
      breadcrumbs={[{ label: session.organization.name }, { label: "Providers" }]}
    >
      {providers.status === "pending" ? <Skeleton height="12rem" /> : null}
      {providers.status === "error" ? (
        <Banner
          tone="critical"
          title="Couldn’t load providers"
          action={
            <Button size="sm" onPress={() => void providers.refetch()}>
              Try again
            </Button>
          }
        >
          The service did not respond.
        </Banner>
      ) : null}
      {providers.status === "success" ? (
        <div style={{ display: "grid", gap: "var(--soa-space-5)" }}>
          <Banner tone="info" title="Health reporting">
            {providers.data.health_note}
          </Banner>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.75rem" }}>
            {providers.data.items.map((provider) => (
              <ProviderCard key={`${provider.capability}:${provider.name}`} provider={provider} />
            ))}
          </ul>

          <section
            aria-label="Routing preview"
            style={{
              border: "1px solid var(--soa-border)",
              borderRadius: "var(--soa-radius-panel)",
              padding: "var(--soa-space-4)",
              display: "grid",
              gap: "0.75rem",
            }}
          >
            <h2 style={{ margin: 0, fontSize: "1rem" }}>Routing preview</h2>
            <div style={{ display: "flex", gap: "1rem", alignItems: "center", flexWrap: "wrap" }}>
              <label>
                Capability{" "}
                <select value={capability} onChange={(event) => setCapability(event.target.value)}>
                  {CAPABILITIES.map((entry) => (
                    <option key={entry} value={entry}>
                      {entry}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={localOnly}
                  onChange={(event) => setLocalOnly(event.target.checked)}
                />{" "}
                Local-only policy
              </label>
              <Button size="sm" onPress={() => preview.mutate()}>
                Preview routing
              </Button>
            </div>
            {preview.status === "success" ? (
              <div>
                <p style={{ margin: "0 0 0.25rem" }}>
                  Order:{" "}
                  <strong>{preview.data.order.join(" → ") || "(no eligible provider)"}</strong>
                </p>
                <ol style={{ margin: 0, paddingLeft: "1.2rem" }}>
                  {preview.data.explanation.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ol>
              </div>
            ) : null}
            {preview.status === "error" ? (
              <Banner tone="critical" title="Preview failed">
                The service did not respond.
              </Banner>
            ) : null}
          </section>
        </div>
      ) : null}
    </AppShell>
  );
}
