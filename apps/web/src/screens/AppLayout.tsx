/**
 * Layout route for /app/$organizationSlug/* (TEN-012).
 *
 * Builds the shell session from /me: organization display data and the
 * caller's effective permissions. Designed states for unauthorized and
 * suspended organizations — never a blank page or a stale prior tenant.
 */

import { Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useLocation, useParams } from "@tanstack/react-router";

import { ApiError, fetchMe } from "../api/client";
import { PermissionDenied } from "../auth/PermissionDenied";
import { DevConsole } from "../components/dev/DevConsole";
import { ShellSessionProvider, type ShellSession } from "../shell/ShellContext";

const SESSION_REFRESH_MS = 60_000;

function CenteredState({ children }: { children: React.ReactNode }) {
  return (
    <main style={{ maxWidth: "32rem", margin: "8vh auto", padding: "0 var(--soa-space-6)" }}>
      {children}
    </main>
  );
}

/** Required read/action permission for every route reachable under the app shell. */
function requiredPermissionForPath(pathname: string, organizationSlug: string): string {
  const prefix = `/app/${organizationSlug}/`;
  const relativePath = pathname.startsWith(prefix) ? pathname.slice(prefix.length) : "";
  const [area, action] = relativePath.split("/");
  switch (area) {
    case "documents":
      return action === "upload" ? "documents.upload" : "documents.read";
    case "review":
      return "documents.review";
    case "processes":
      return "processes.read";
    case "streams":
    case "providers":
      return "streams.read";
    case "catalogs":
      return "catalogs.read";
    case "integrations":
      return "integrations.read";
    case "jobs":
      return "jobs.read";
    case "analytics":
      return "analytics.read";
    case "audit":
      return "audit.read";
    case "settings":
      return "organization.manage";
    case "overview":
    case "getting-started":
    case "support":
    default:
      return "organization.read";
  }
}

export function AppLayout() {
  const { organizationSlug } = useParams({ strict: false }) as { organizationSlug: string };
  const location = useLocation();
  const { data, error, status, refetch } = useQuery({
    queryKey: ["me"],
    queryFn: fetchMe,
    staleTime: 30_000,
    // Authorization context is small and security-sensitive. Always refresh
    // it on focus (even inside staleTime), and periodically while this tab is
    // active so membership suspension or permission reduction is bounded
    // without making every feature query revalidate the session itself.
    refetchOnWindowFocus: "always",
    refetchInterval: SESSION_REFRESH_MS,
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
    const denied = error instanceof ApiError && error.status === 403;
    return (
      <CenteredState>
        <Banner
          tone="critical"
          title={denied ? "Permission denied" : "Couldn’t load your session"}
          action={
            <Button size="sm" onPress={() => void refetch()}>
              Try again
            </Button>
          }
        >
          {denied
            ? "Your identity is valid, but it is not allowed to load organization memberships."
            : "The identity service did not respond. Nothing has been changed."}
          {error instanceof ApiError && error.correlationId ? (
            <p style={{ marginBottom: 0 }}>
              Support reference: <code>{error.correlationId}</code>
            </p>
          ) : null}
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
    userId: data.user_id,
    availableOrganizations: data.memberships
      .filter((m) => m.status === "active" && m.organization_status === "active")
      .map((m) => ({ slug: m.organization_slug, name: m.organization_name })),
  };
  const requiredPermission = requiredPermissionForPath(location.pathname, organizationSlug);
  const denied = !session.permissions.has(requiredPermission);

  // Key by organization so a switch unmounts the previous tenant's subtree
  // immediately — no stale prior-tenant data can flash.
  return (
    <ShellSessionProvider key={membership.organization_id} session={session}>
      {denied ? <PermissionDenied requiredPermission={requiredPermission} /> : <Outlet />}
      {import.meta.env.MODE === "development" && session.permissions.has("jobs.read") && (
        <>
          {/* The console is position:fixed; the spacer keeps its bar from
              occluding the bottom of the page content. */}
          <div aria-hidden="true" style={{ height: 32 }} />
          <DevConsole organizationSlug={membership.organization_slug} />
        </>
      )}
    </ShellSessionProvider>
  );
}
