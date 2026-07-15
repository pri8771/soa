/**
 * Organization selection (TEN-012, UI_UX_BLUEPRINT §6.1).
 *
 * Shows only organizations where the caller holds an ACTIVE membership.
 * Suspended organizations render with their status and are not enterable.
 */

import { Badge, Banner, Button, Skeleton } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { fetchMe } from "../api/client";

export function SelectOrganization() {
  const { data, status, refetch } = useQuery({ queryKey: ["me"], queryFn: fetchMe });

  return (
    <main style={{ maxWidth: "32rem", margin: "8vh auto", padding: "0 var(--soa-space-6)" }}>
      <h1 style={{ font: "var(--soa-font-heading-xl)" }}>Choose an organization</h1>
      <p style={{ display: "flex", gap: "var(--soa-space-4)", flexWrap: "wrap" }}>
        <Link to="/create-organization">Create an organization</Link>
        <Link to="/accept-invitation">Accept an invitation</Link>
      </p>

      {status === "pending" ? (
        <div
          style={{ display: "grid", gap: "var(--soa-space-3)", marginTop: "var(--soa-space-4)" }}
        >
          <Skeleton height="3.5rem" />
          <Skeleton height="3.5rem" />
        </div>
      ) : null}

      {status === "error" ? (
        <Banner
          tone="critical"
          title="Couldn’t load your organizations"
          action={
            <Button size="sm" onPress={() => void refetch()}>
              Try again
            </Button>
          }
        >
          Your session may have expired. Nothing has been changed.
        </Banner>
      ) : null}

      {status === "success" ? (
        <ul
          style={{
            listStyle: "none",
            margin: "var(--soa-space-4) 0 0",
            padding: 0,
            display: "grid",
            gap: "var(--soa-space-3)",
          }}
        >
          {data.memberships.filter((m) => m.status === "active").length === 0 ? (
            <li>
              <Banner tone="info" title="No organizations yet">
                You are not an active member of any organization. Accept an invitation or create a
                new organization to begin.
              </Banner>
            </li>
          ) : null}
          {data.memberships
            .filter((membership) => membership.status === "active")
            .map((membership) => {
              const operational = membership.organization_status === "active";
              return (
                <li
                  key={membership.organization_id}
                  style={{
                    border: "1px solid var(--soa-border)",
                    borderRadius: "var(--soa-radius-panel)",
                    padding: "var(--soa-space-4)",
                    background: "var(--soa-surface)",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "var(--soa-space-4)",
                  }}
                >
                  <div>
                    <p style={{ margin: 0, font: "var(--soa-font-heading-md)" }}>
                      {membership.organization_name}
                    </p>
                    <p style={{ margin: 0, font: "var(--soa-font-caption)" }}>
                      {membership.organization_slug}
                    </p>
                  </div>
                  {operational ? (
                    <Link
                      to="/app/$organizationSlug/overview"
                      params={{ organizationSlug: membership.organization_slug }}
                      style={{ font: "var(--soa-font-body-md)", fontWeight: 600 }}
                    >
                      Open
                    </Link>
                  ) : (
                    <Badge tone="warning">{membership.organization_status}</Badge>
                  )}
                </li>
              );
            })}
        </ul>
      ) : null}
    </main>
  );
}
