/**
 * Layout route for /app/$organizationSlug/* (TEN-012).
 *
 * Builds the shell session from /me: organization display data and the
 * caller's effective permissions. Designed states for unauthorized and
 * suspended organizations — never a blank page or a stale prior tenant.
 */

import { Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useParams } from "@tanstack/react-router";

import { fetchMe } from "../api/client";
import { ShellSessionProvider, type ShellSession } from "../shell/ShellContext";

function CenteredState({ children }: { children: React.ReactNode }) {
  return (
    <main style={{ maxWidth: "32rem", margin: "8vh auto", padding: "0 var(--soa-space-6)" }}>
      {children}
    </main>
  );
}

export function AppLayout() {
  const { organizationSlug } = useParams({ strict: false }) as { organizationSlug: string };
  const { data, status, refetch } = useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    staleTime: 30_000,
  });

  if (status === "pending") {
    return (
      <CenteredState>
        <Skeleton height="2rem" width="60%" />
        <div style={{ marginTop: "var(--soa-space-4)" }}>
          <Skeleton height="8rem" />
        </div>
      </CenteredState>
    );
  }

  if (status === "error") {
    return (
      <CenteredState>
        <Banner
          tone="critical"
          title="Couldn’t load your session"
          action={
            <Button size="sm" onPress={() => void refetch()}>
              Try again
            </Button>
          }
        >
          The identity service did not respond. Nothing has been changed.
        </Banner>
      </CenteredState>
    );
  }

  const membership = data.memberships.find(
    (candidate) => candidate.organization_slug === organizationSlug,
  );

  if (!membership || membership.status !== "active") {
    return (
      <CenteredState>
        <Banner tone="warning" title="You don’t have access to this organization">
          Your account has no active membership in “{organizationSlug}”. Choose another
          organization, or ask an administrator for an invitation.
        </Banner>
        <p style={{ marginTop: "var(--soa-space-4)" }}>
          <Link to="/select-organization">Choose an organization</Link>
        </p>
      </CenteredState>
    );
  }

  if (membership.organization_status !== "active") {
    return (
      <CenteredState>
        <Banner tone="critical" title="This organization is suspended">
          “{membership.organization_name}” is currently {membership.organization_status}. Contact
          your organization administrator or SOA support.
        </Banner>
        <p style={{ marginTop: "var(--soa-space-4)" }}>
          <Link to="/select-organization">Choose another organization</Link>
        </p>
      </CenteredState>
    );
  }

  const session: ShellSession = {
    organization: {
      slug: membership.organization_slug,
      name: membership.organization_name,
    },
    permissions: new Set(membership.permissions),
    devSession: data.dev_session,
    userLabel: data.email,
    availableOrganizations: data.memberships
      .filter((m) => m.status === "active" && m.organization_status === "active")
      .map((m) => ({ slug: m.organization_slug, name: m.organization_name })),
  };

  // Key by organization so a switch unmounts the previous tenant's subtree
  // immediately — no stale prior-tenant data can flash.
  return (
    <ShellSessionProvider key={membership.organization_id} session={session}>
      <Outlet />
    </ShellSessionProvider>
  );
}
