/**
 * Layout route for /app/$organizationSlug/*.
 *
 * Provides the shell session. TEN-012 replaces the static development
 * session below with real data from /me and the organization APIs
 * (memberships, permissions, organization display name).
 */

import { Outlet, useParams } from "@tanstack/react-router";

import { NAV_ITEMS } from "../shell/AppShell";
import { ShellSessionProvider, type ShellSession } from "../shell/ShellContext";

export function AppLayout() {
  const { organizationSlug } = useParams({ strict: false }) as { organizationSlug: string };
  const session: ShellSession = {
    organization: { slug: organizationSlug, name: organizationSlug },
    permissions: new Set(NAV_ITEMS.map((item) => item.permission)),
    devSession: true,
    userLabel: "development session",
  };
  return (
    <ShellSessionProvider session={session}>
      <Outlet />
    </ShellSessionProvider>
  );
}
