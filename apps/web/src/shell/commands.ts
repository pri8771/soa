/**
 * Command registry for the palette (DSN-005).
 *
 * Commands are permission-gated and organization-scoped. Future features
 * plug in by contributing commands (and later, search results) through
 * ``registerCommands`` — the palette itself never hard-codes feature
 * knowledge beyond primary navigation.
 */

import { NAV_ITEMS } from "./AppShell";
import type { ShellSession } from "./ShellContext";

export interface Command {
  id: string;
  label: string;
  section: "Navigation" | "Actions";
  keywords?: string;
  permission?: string;
  perform: () => void;
}

export type Navigate = (to: string, params: Record<string, string>) => void;

export function buildNavigationCommands(session: ShellSession, navigate: Navigate): Command[] {
  return NAV_ITEMS.filter((item) => session.permissions.has(item.permission)).map((item) => ({
    id: `nav:${item.label.toLowerCase()}`,
    label: `Go to ${item.label}`,
    section: "Navigation" as const,
    keywords: item.label.toLowerCase(),
    permission: item.permission,
    perform: () => navigate(item.to, { organizationSlug: session.organization.slug }),
  }));
}

/** Extra commands contributed by features (pluggable). */
export function filterCommands(commands: Command[], query: string): Command[] {
  const needle = query.trim().toLowerCase();
  if (!needle) {
    return commands;
  }
  return commands.filter(
    (command) =>
      command.label.toLowerCase().includes(needle) || (command.keywords ?? "").includes(needle),
  );
}
