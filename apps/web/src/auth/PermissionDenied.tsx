import { Banner } from "@soa/design-system";
import { Link } from "@tanstack/react-router";

import { AppShell } from "../shell/AppShell";
import { useShellSession } from "../shell/ShellContext";

export function PermissionDenied({
  requiredPermission,
  correlationId,
}: {
  requiredPermission: string;
  correlationId?: string | null;
}) {
  const session = useShellSession();
  return (
    <AppShell title="Permission denied" breadcrumbs={[{ label: session.organization.name }]}>
      <div style={{ maxWidth: "40rem" }}>
        <Banner tone="warning" title="You can’t open this area">
          Your membership does not include <code>{requiredPermission}</code>. Ask an organization
          administrator to update your role, or switch to an organization where you have access.
          {correlationId ? (
            <p style={{ marginBottom: 0 }}>
              Support reference: <code>{correlationId}</code>
            </p>
          ) : null}
        </Banner>
        <p style={{ display: "flex", gap: "var(--soa-space-4)" }}>
          <Link
            to="/app/$organizationSlug/overview"
            params={{ organizationSlug: session.organization.slug }}
          >
            Go to overview
          </Link>
          <Link to="/select-organization">Choose another organization</Link>
        </p>
      </div>
    </AppShell>
  );
}
