import { Banner, Button, TextField } from "@soa/design-system";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { ApiError, createOrganization } from "../api/client";

function slugFromName(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 100);
}

export function CreateOrganization() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const create = useMutation({
    mutationFn: () => createOrganization(name.trim(), slug.trim()),
    onSuccess: async (organization) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      await navigate({
        to: "/app/$organizationSlug/getting-started",
        params: { organizationSlug: organization.slug },
        replace: true,
      });
    },
  });

  return (
    <main style={{ maxWidth: "32rem", margin: "8vh auto", padding: "0 var(--soa-space-6)" }}>
      <h1 style={{ font: "var(--soa-font-heading-xl)" }}>Create an organization</h1>
      <p>Organizations isolate members, configuration, documents, integrations, and audit data.</p>
      {create.isError ? (
        <Banner tone="critical" title="Organization wasn’t created">
          {create.error.message}
          {create.error instanceof ApiError && create.error.correlationId ? (
            <span>
              {" "}
              Support reference: <code>{create.error.correlationId}</code>.
            </span>
          ) : null}
        </Banner>
      ) : null}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate();
        }}
        style={{ display: "grid", gap: "var(--soa-space-4)", marginTop: "var(--soa-space-5)" }}
      >
        <TextField
          label="Organization name"
          value={name}
          onChange={(value) => {
            setName(value);
            if (!slugEdited) setSlug(slugFromName(value));
          }}
          isRequired
        />
        <TextField
          label="Organization slug"
          description="Used in URLs. Lowercase letters, numbers, and hyphens only."
          value={slug}
          onChange={(value) => {
            setSlugEdited(true);
            setSlug(value.toLowerCase());
          }}
          isRequired
        />
        <div style={{ display: "flex", gap: "var(--soa-space-3)" }}>
          <Button
            type="submit"
            variant="primary"
            isDisabled={!name.trim() || !/^[a-z0-9][a-z0-9-]+$/.test(slug) || create.isPending}
          >
            Create organization
          </Button>
          <Link to="/select-organization">Cancel</Link>
        </div>
      </form>
    </main>
  );
}
