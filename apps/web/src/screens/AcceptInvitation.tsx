import { Banner, Button, TextField } from "@soa/design-system";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";

import { ApiError, acceptInvitation } from "../api/client";

export function AcceptInvitation() {
  const search = useSearch({ strict: false }) as { organization?: string };
  const [organization, setOrganization] = useState(search.organization ?? "");
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const accept = useMutation({
    mutationFn: () => acceptInvitation(organization.trim()),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      await navigate({
        to: "/app/$organizationSlug/skills",
        params: { organizationSlug: organization.trim() },
        replace: true,
      });
    },
  });

  return (
    <main style={{ maxWidth: "32rem", margin: "8vh auto", padding: "0 var(--soa-space-6)" }}>
      <h1 style={{ font: "var(--soa-font-heading-xl)" }}>Accept invitation</h1>
      <p>
        Sign-in email matching is enforced by the server. Accepting grants only the roles assigned
        by the organization administrator.
      </p>
      {accept.isError ? (
        <Banner tone="critical" title="Invitation couldn’t be accepted">
          {accept.error.message}
          {accept.error instanceof ApiError && accept.error.correlationId ? (
            <span>
              {" "}
              Support reference: <code>{accept.error.correlationId}</code>.
            </span>
          ) : null}
        </Banner>
      ) : null}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          accept.mutate();
        }}
        style={{ display: "grid", gap: "var(--soa-space-4)", marginTop: "var(--soa-space-5)" }}
      >
        <TextField
          label="Organization slug"
          value={organization}
          onChange={setOrganization}
          isRequired
        />
        <div style={{ display: "flex", gap: "var(--soa-space-3)" }}>
          <Button
            type="submit"
            variant="primary"
            isDisabled={!organization.trim() || accept.isPending}
          >
            Accept invitation
          </Button>
          <Link to="/select-organization">Cancel</Link>
        </div>
      </form>
    </main>
  );
}
