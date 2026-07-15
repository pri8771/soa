/**
 * Provider administration (AIO-019): the shipped provider catalog with
 * capability, region, languages, health, data policy, and the tenant's
 * approval/credential state, write-only credential rotation, and the
 * validated draft/publish lifecycle for provider and confidence policy.
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  createAdminPolicyDraft,
  fetchAdminPolicies,
  fetchProviderCredentials,
  fetchProviders,
  previewProviderRouting,
  publishAdminPolicy,
  revokeProviderCredential,
  setProviderCredential,
  validateAdminPolicy,
  type AdminPolicyType,
  type AdminPolicyVersion,
  type ProviderCredentialEntry,
  type ProviderEntry,
} from "../api/client";
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
          Credential: {provider.credential_configured ? "managed and server-bound" : "not needed"}.
          Secret values and secret-store references are never displayed.
        </p>
      ) : null}
    </li>
  );
}

const panelStyle = {
  border: "1px solid var(--soa-border)",
  borderRadius: "var(--soa-radius-panel)",
  padding: "var(--soa-space-4)",
  display: "grid",
  gap: "0.9rem",
} as const;

function PolicyHistory({
  title,
  items,
  onValidate,
  onPublish,
  busy,
}: {
  title: string;
  items: AdminPolicyVersion[];
  onValidate: (policy: AdminPolicyVersion) => void;
  onPublish: (policy: AdminPolicyVersion) => void;
  busy: boolean;
}) {
  return (
    <div style={{ display: "grid", gap: "0.5rem" }}>
      <h3 style={{ margin: 0, fontSize: "0.95rem" }}>{title}</h3>
      {items.length === 0 ? (
        <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>No versions yet.</p>
      ) : (
        <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.5rem" }}>
          {[...items].reverse().map((policy) => (
            <li
              key={policy.id}
              style={{
                borderTop: "1px solid var(--soa-border)",
                paddingTop: "0.5rem",
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "0.75rem",
                flexWrap: "wrap",
              }}
            >
              <span>
                v{policy.version_number} · <strong>{policy.state}</strong>
                {policy.change_summary ? ` · ${policy.change_summary}` : ""}
              </span>
              {policy.state === "draft" ? (
                <span style={{ display: "flex", gap: "0.5rem" }}>
                  <Button
                    size="sm"
                    variant="subtle"
                    isDisabled={busy}
                    onPress={() => onValidate(policy)}
                  >
                    Validate
                  </Button>
                  <Button size="sm" isDisabled={busy} onPress={() => onPublish(policy)}>
                    Publish
                  </Button>
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ProviderAdministration({ slug, catalog }: { slug: string; catalog: ProviderEntry[] }) {
  const session = useShellSession();
  const queryClient = useQueryClient();
  const canManagePolicy = session.permissions.has("streams.manage");
  const canManageCredentials = canManagePolicy && session.permissions.has("credentials.manage");
  const credentials = useQuery({
    queryKey: ["provider-credentials", slug],
    queryFn: () => fetchProviderCredentials(slug),
    enabled: canManageCredentials,
  });
  const providerPolicies = useQuery({
    queryKey: ["admin-policies", slug, "provider"],
    queryFn: () => fetchAdminPolicies(slug, "provider"),
  });
  const confidencePolicies = useQuery({
    queryKey: ["admin-policies", slug, "confidence"],
    queryFn: () => fetchAdminPolicies(slug, "confidence"),
  });

  const hostedProviders = catalog.filter((provider) =>
    provider.availability.includes("tenant_credential"),
  );
  const extractionProviders = catalog.filter(
    (provider) => provider.capability === "field_extraction" && provider.name !== "mock",
  );
  const [credentialProvider, setCredentialProvider] = useState(
    hostedProviders[0]?.name ?? "anthropic-claude",
  );
  const [credentialLabel, setCredentialLabel] = useState("Production credential");
  const [credentialSecret, setCredentialSecret] = useState("");
  const storeCredential = useMutation({
    mutationFn: () =>
      setProviderCredential(slug, credentialProvider, {
        label: credentialLabel,
        kind: "api_key",
        secret: credentialSecret,
      }),
    onSuccess: async () => {
      setCredentialSecret("");
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["provider-credentials", slug] }),
        queryClient.invalidateQueries({ queryKey: ["providers", slug] }),
      ]);
    },
  });

  const currentCredentials = new Map(
    (credentials.data?.items ?? [])
      .filter((credential) => credential.status === "current")
      .map((credential) => [credential.provider_name, credential]),
  );
  const [primaryProvider, setPrimaryProvider] = useState(
    extractionProviders.find((provider) => provider.local)?.name ??
      extractionProviders[0]?.name ??
      "local-openai-compatible",
  );
  const [fallbackProvider, setFallbackProvider] = useState("");
  const [localOnly, setPolicyLocalOnly] = useState(true);
  const [allowThirdParty, setAllowThirdParty] = useState(false);
  const [providerSummary, setProviderSummary] = useState("Update provider routing");
  const providerSelection = (providerName: string): Record<string, unknown> => {
    const selected = catalog.find((provider) => provider.name === providerName);
    const credential = currentCredentials.get(providerName);
    return {
      provider_name: providerName,
      ...(selected?.availability.includes("tenant_credential") && credential
        ? { credential_id: credential.id }
        : {}),
    };
  };
  const createProviderPolicy = useMutation({
    mutationFn: () => {
      const selectedNames = [primaryProvider, fallbackProvider].filter(Boolean);
      const hosted = selectedNames
        .map((name) => catalog.find((provider) => provider.name === name))
        .filter((provider): provider is ProviderEntry => provider !== undefined && !provider.local);
      return createAdminPolicyDraft(slug, "provider", {
        change_summary: providerSummary.trim() || null,
        definition: {
          ...providerSelection(primaryProvider),
          capabilities: ["ocr", "field_extraction"],
          fallback_providers: fallbackProvider ? [providerSelection(fallbackProvider)] : [],
          local_only: localOnly,
          allow_third_party_processing: allowThirdParty,
          ...(hosted.length > 0
            ? { allowed_regions: [...new Set(hosted.map((provider) => provider.region))] }
            : {}),
        },
      });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["admin-policies", slug, "provider"] });
    },
  });

  const [confidenceFloor, setConfidenceFloor] = useState("0.85");
  const [criticalFloor, setCriticalFloor] = useState("0.98");
  const [confidenceSummary, setConfidenceSummary] = useState("Update review thresholds");
  const createConfidencePolicy = useMutation({
    mutationFn: () =>
      createAdminPolicyDraft(slug, "confidence", {
        change_summary: confidenceSummary.trim() || null,
        definition: {
          floor: Number(confidenceFloor),
          critical_floor: Number(criticalFloor),
          field_overrides: {},
          critical_requires_evidence: true,
          critical_candidate_margin: 0.2,
          standard_candidate_margin: 0.05,
          review_on_indeterminate_error_rules: true,
        },
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["admin-policies", slug, "confidence"] });
    },
  });

  const validatePolicy = useMutation({
    mutationFn: ({ type, id }: { type: AdminPolicyType; id: string }) =>
      validateAdminPolicy(slug, type, id),
  });
  const publishPolicy = useMutation({
    mutationFn: ({ type, id }: { type: AdminPolicyType; id: string }) =>
      publishAdminPolicy(slug, type, id),
    onSuccess: async (policy) => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["admin-policies", slug, policy.policy_type],
        }),
        queryClient.invalidateQueries({ queryKey: ["providers", slug] }),
      ]);
    },
  });

  const revocableCredentials = (credentials.data?.items ?? []).filter(
    (credential) => credential.status !== "revoked",
  );
  const [revokeCredentialId, setRevokeCredentialId] = useState("");
  const [revokeReason, setRevokeReason] = useState("");
  const [forceRevoke, setForceRevoke] = useState(false);
  const [forceConfirmation, setForceConfirmation] = useState("");
  const revokeCredential = useMutation({
    mutationFn: () =>
      revokeProviderCredential(slug, revokeCredentialId, {
        reason: revokeReason,
        force: forceRevoke,
        confirmation: forceRevoke ? forceConfirmation : null,
      }),
    onSuccess: async () => {
      setRevokeReason("");
      setForceRevoke(false);
      setForceConfirmation("");
      await queryClient.invalidateQueries({ queryKey: ["provider-credentials", slug] });
    },
  });

  const policyBusy = validatePolicy.isPending || publishPolicy.isPending;
  const error =
    storeCredential.error ??
    createProviderPolicy.error ??
    createConfidencePolicy.error ??
    validatePolicy.error ??
    publishPolicy.error ??
    revokeCredential.error;

  if (!canManagePolicy) {
    return (
      <Banner tone="info" title="Read-only provider access">
        streams.manage is required to create or publish policy versions.
      </Banner>
    );
  }

  return (
    <section
      aria-label="Provider and confidence administration"
      style={{ display: "grid", gap: "1rem" }}
    >
      <h2 style={{ margin: 0 }}>Provider and confidence administration</h2>
      <Banner tone="warning" title="Immutable, write-only configuration">
        Secret values are sent once to the managed secret store. Rotation retains the old value for
        published policy and run pins; explicit revocation is a separate audited action.
      </Banner>
      {error ? (
        <Banner tone="critical" title="Configuration change failed">
          {error.message}
        </Banner>
      ) : null}
      {storeCredential.data ? (
        <Banner tone="success" title="Credential stored">
          {storeCredential.data.detail}
        </Banner>
      ) : null}
      {createProviderPolicy.data ? (
        <Banner
          tone="success"
          title={`Provider draft v${createProviderPolicy.data.version_number} created`}
        >
          Validate the server-bound draft, then publish it when the findings are clear.
        </Banner>
      ) : null}
      {createConfidencePolicy.data ? (
        <Banner
          tone="success"
          title={`Confidence draft v${createConfidencePolicy.data.version_number} created`}
        >
          Validate the thresholds before publishing.
        </Banner>
      ) : null}
      {publishPolicy.data ? (
        <Banner tone="success" title={`${publishPolicy.data.policy_type} policy published`}>
          Version {publishPolicy.data.version_number} is immutable and active for newly published
          process configuration.
        </Banner>
      ) : null}
      {revokeCredential.data ? (
        <Banner tone="success" title="Credential revocation queued">
          {revokeCredential.data.detail}
        </Banner>
      ) : null}
      {validatePolicy.data ? (
        <Banner
          tone={validatePolicy.data.valid ? "success" : "critical"}
          title={validatePolicy.data.valid ? "Draft is valid" : "Draft is not valid"}
        >
          {validatePolicy.data.findings.length === 0
            ? "All server-side policy, catalog, and credential checks passed."
            : validatePolicy.data.findings.map((finding) => finding.message).join(" ")}
        </Banner>
      ) : null}

      {canManageCredentials ? (
        <div style={panelStyle}>
          <h3 style={{ margin: 0 }}>Store or rotate a hosted-provider credential</h3>
          <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
            <label>
              Provider{" "}
              <select
                value={credentialProvider}
                onChange={(event) => setCredentialProvider(event.target.value)}
              >
                {hostedProviders.map((provider) => (
                  <option key={provider.name} value={provider.name}>
                    {provider.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Label{" "}
              <input
                value={credentialLabel}
                maxLength={100}
                onChange={(event) => setCredentialLabel(event.target.value)}
              />
            </label>
            <label>
              API key{" "}
              <input
                aria-label="Provider API key"
                type="password"
                autoComplete="new-password"
                value={credentialSecret}
                onChange={(event) => setCredentialSecret(event.target.value)}
              />
            </label>
            <Button
              size="sm"
              isDisabled={
                credentialSecret.length < 8 || !credentialLabel.trim() || storeCredential.isPending
              }
              onPress={() => storeCredential.mutate()}
            >
              {currentCredentials.has(credentialProvider)
                ? "Rotate credential"
                : "Store credential"}
            </Button>
          </div>
          <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
            This form has no read-back path. Leaving or refreshing the page cannot recover a key.
          </p>
        </div>
      ) : null}

      <div style={panelStyle}>
        <h3 style={{ margin: 0 }}>Create provider-policy draft</h3>
        <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", alignItems: "center" }}>
          <label>
            Primary{" "}
            <select
              value={primaryProvider}
              onChange={(event) => setPrimaryProvider(event.target.value)}
            >
              {extractionProviders.map((provider) => (
                <option key={provider.name} value={provider.name}>
                  {provider.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Fallback{" "}
            <select
              value={fallbackProvider}
              onChange={(event) => setFallbackProvider(event.target.value)}
            >
              <option value="">No fallback</option>
              {extractionProviders
                .filter((provider) => provider.name !== primaryProvider)
                .map((provider) => (
                  <option key={provider.name} value={provider.name}>
                    {provider.name}
                  </option>
                ))}
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={localOnly}
              onChange={(event) => setPolicyLocalOnly(event.target.checked)}
            />{" "}
            Local only
          </label>
          <label>
            <input
              type="checkbox"
              checked={allowThirdParty}
              onChange={(event) => setAllowThirdParty(event.target.checked)}
            />{" "}
            Allow third-party processing
          </label>
          <label>
            Change summary{" "}
            <input
              value={providerSummary}
              maxLength={500}
              onChange={(event) => setProviderSummary(event.target.value)}
            />
          </label>
          <Button
            size="sm"
            isDisabled={createProviderPolicy.isPending || !primaryProvider}
            onPress={() => createProviderPolicy.mutate()}
          >
            Create provider draft
          </Button>
        </div>
        <PolicyHistory
          title="Provider-policy versions"
          items={providerPolicies.data?.items ?? []}
          busy={policyBusy}
          onValidate={(policy) => validatePolicy.mutate({ type: "provider", id: policy.id })}
          onPublish={(policy) => publishPolicy.mutate({ type: "provider", id: policy.id })}
        />
      </div>

      <div style={panelStyle}>
        <h3 style={{ margin: 0 }}>Create confidence-policy draft</h3>
        <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", alignItems: "center" }}>
          <label>
            Standard floor{" "}
            <input
              aria-label="Standard confidence floor"
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={confidenceFloor}
              onChange={(event) => setConfidenceFloor(event.target.value)}
            />
          </label>
          <label>
            Critical floor{" "}
            <input
              aria-label="Critical confidence floor"
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={criticalFloor}
              onChange={(event) => setCriticalFloor(event.target.value)}
            />
          </label>
          <label>
            Change summary{" "}
            <input
              value={confidenceSummary}
              maxLength={500}
              onChange={(event) => setConfidenceSummary(event.target.value)}
            />
          </label>
          <Button
            size="sm"
            isDisabled={createConfidencePolicy.isPending}
            onPress={() => createConfidencePolicy.mutate()}
          >
            Create confidence draft
          </Button>
        </div>
        <PolicyHistory
          title="Confidence-policy versions"
          items={confidencePolicies.data?.items ?? []}
          busy={policyBusy}
          onValidate={(policy) => validatePolicy.mutate({ type: "confidence", id: policy.id })}
          onPublish={(policy) => publishPolicy.mutate({ type: "confidence", id: policy.id })}
        />
      </div>

      {canManageCredentials && revocableCredentials.length > 0 ? (
        <div style={panelStyle}>
          <h3 style={{ margin: 0 }}>Revoke a stored credential</h3>
          <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap", alignItems: "center" }}>
            <label>
              Credential{" "}
              <select
                value={revokeCredentialId}
                onChange={(event) => setRevokeCredentialId(event.target.value)}
              >
                <option value="">Select credential</option>
                {revocableCredentials.map((credential: ProviderCredentialEntry) => (
                  <option key={credential.id} value={credential.id}>
                    {credential.provider_name} · {credential.label} · {credential.status}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Reason{" "}
              <input
                value={revokeReason}
                maxLength={500}
                onChange={(event) => setRevokeReason(event.target.value)}
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={forceRevoke}
                onChange={(event) => setForceRevoke(event.target.checked)}
              />{" "}
              Emergency force revoke
            </label>
            {forceRevoke ? (
              <label>
                Type REVOKE{" "}
                <input
                  aria-label="Force revocation confirmation"
                  value={forceConfirmation}
                  onChange={(event) => setForceConfirmation(event.target.value)}
                />
              </label>
            ) : null}
            <Button
              size="sm"
              variant="secondary"
              isDisabled={
                !revokeCredentialId ||
                !revokeReason.trim() ||
                (forceRevoke && forceConfirmation !== "REVOKE") ||
                revokeCredential.isPending
              }
              onPress={() => revokeCredential.mutate()}
            >
              Queue revocation
            </Button>
          </div>
          <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>
            Normal revocation refuses any policy reference. Force is break-glass: pinned work using
            the compromised key will fail closed.
          </p>
        </div>
      ) : null}
    </section>
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

          <ProviderAdministration slug={slug} catalog={providers.data.items} />

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
