/**
 * Shell context: current organization + the principal's permissions.
 *
 * DSN-004 defines the contract; TEN-012 populates it from /me and the
 * organization APIs. Navigation filters itself by these permissions.
 */

import { createContext, useContext, type ReactNode } from "react";

export interface ShellOrganization {
  slug: string;
  name: string;
}

export interface ShellSession {
  organization: ShellOrganization;
  permissions: ReadonlySet<string>;
  devSession: boolean;
  userLabel: string;
  /** Other organizations the user can switch to (active memberships only). */
  availableOrganizations?: ShellOrganization[];
}

const ShellSessionContext = createContext<ShellSession | null>(null);

export function ShellSessionProvider({
  session,
  children,
}: {
  session: ShellSession;
  children: ReactNode;
}) {
  return <ShellSessionContext.Provider value={session}>{children}</ShellSessionContext.Provider>;
}

export function useShellSession(): ShellSession {
  const session = useContext(ShellSessionContext);
  if (session === null) {
    throw new Error("useShellSession requires a <ShellSessionProvider> ancestor");
  }
  return session;
}
