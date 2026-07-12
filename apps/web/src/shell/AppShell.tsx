/**
 * Application shell (DSN-004, UI_UX_BLUEPRINT §3).
 *
 * Left navigation (248px expanded / 72px collapsed, persisted), 56px top
 * context bar with an always-visible organization display, page header with
 * breadcrumbs, and permission-aware navigation: items the principal cannot
 * use are not rendered at all.
 */

import { Badge, Button } from "@soa/design-system";
import { Link } from "@tanstack/react-router";
import { useEffect, useState, type ReactNode } from "react";

import { CommandPalette } from "./CommandPalette";
import { OrganizationSwitcher } from "./OrganizationSwitcher";
import { useShellSession } from "./ShellContext";
import "./shell.css";

/** Primary navigation, in blueprint order (§3.1). */
export const NAV_ITEMS = [
  {
    label: "Overview",
    to: "/app/$organizationSlug/overview",
    permission: "organization.read",
    icon: "◫",
  },
  {
    label: "Documents",
    to: "/app/$organizationSlug/documents",
    permission: "documents.read",
    icon: "▤",
  },
  {
    label: "Review",
    to: "/app/$organizationSlug/review",
    permission: "documents.review",
    icon: "✓",
  },
  {
    label: "Processes",
    to: "/app/$organizationSlug/processes",
    permission: "processes.read",
    icon: "⧉",
  },
  {
    label: "Streams",
    to: "/app/$organizationSlug/streams",
    permission: "streams.read",
    icon: "≋",
  },
  {
    label: "Catalogs",
    to: "/app/$organizationSlug/catalogs",
    permission: "catalogs.read",
    icon: "☰",
  },
  {
    label: "Integrations",
    to: "/app/$organizationSlug/integrations",
    permission: "integrations.read",
    icon: "⇄",
  },
  {
    label: "Jobs",
    to: "/app/$organizationSlug/jobs",
    permission: "jobs.read",
    icon: "⏱",
  },
  {
    label: "Analytics",
    to: "/app/$organizationSlug/analytics",
    permission: "analytics.read",
    icon: "∿",
  },
  {
    label: "Settings",
    to: "/app/$organizationSlug/settings",
    permission: "organization.manage",
    icon: "⚙",
  },
] as const;

export type NavItem = (typeof NAV_ITEMS)[number];

const NAV_COLLAPSE_KEY = "soa.nav.collapsed";

export interface Breadcrumb {
  label: string;
  to?: string;
}

export function AppShell({
  title,
  breadcrumbs = [],
  actions,
  children,
}: {
  title: string;
  breadcrumbs?: Breadcrumb[];
  actions?: ReactNode;
  children: ReactNode;
}) {
  const session = useShellSession();
  const [collapsed, setCollapsed] = useState<boolean>(
    () => globalThis.localStorage?.getItem(NAV_COLLAPSE_KEY) === "true",
  );

  useEffect(() => {
    globalThis.localStorage?.setItem(NAV_COLLAPSE_KEY, String(collapsed));
  }, [collapsed]);

  const visibleItems = NAV_ITEMS.filter((item) => session.permissions.has(item.permission));

  return (
    <div className="soa-shell" data-nav-collapsed={collapsed}>
      <nav className="soa-shell-nav" aria-label="Primary">
        <Link to="/" className="soa-shell-brand">
          <span aria-hidden="true" className="soa-shell-brand-mark">
            S
          </span>
          <span className="soa-shell-brand-name">SOA</span>
        </Link>
        {visibleItems.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            params={{ organizationSlug: session.organization.slug }}
            className="soa-shell-nav-item"
            activeProps={{ "data-status": "active", "aria-current": "page" } as never}
          >
            <span aria-hidden="true">{item.icon}</span>
            <span className="soa-shell-nav-label">{item.label}</span>
          </Link>
        ))}
        <div className="soa-shell-nav-footer">
          <Button variant="subtle" size="sm" onPress={() => setCollapsed((value) => !value)}>
            <span aria-hidden="true">{collapsed ? "»" : "«"}</span>
            <span className="soa-shell-nav-label">
              {collapsed ? "Expand navigation" : "Collapse navigation"}
            </span>
          </Button>
        </div>
      </nav>

      <header className="soa-shell-topbar">
        <OrganizationSwitcher />
        {session.devSession ? (
          <Badge tone="warning" className="soa-shell-dev-badge">
            Development identity
          </Badge>
        ) : null}
        <div className="soa-shell-topbar-spacer" />
        <CommandPalette />
        <span style={{ font: "var(--soa-font-body-sm)", color: "var(--soa-text-secondary)" }}>
          {session.userLabel}
        </span>
      </header>

      <div className="soa-shell-main">
        <div className="soa-page-header">
          {breadcrumbs.length > 0 ? (
            <nav aria-label="Breadcrumb">
              <ol className="soa-breadcrumbs">
                {breadcrumbs.map((crumb, index) => (
                  <li key={crumb.label}>
                    {/* Breadcrumb targets are validated by route tests, not the type system. */}
                    {crumb.to ? <Link to={crumb.to as "/"}>{crumb.label}</Link> : crumb.label}
                    {index < breadcrumbs.length - 1 ? <span aria-hidden="true"> / </span> : null}
                  </li>
                ))}
              </ol>
            </nav>
          ) : null}
          <div className="soa-page-header-row">
            <h1 className="soa-page-title">{title}</h1>
            {actions ? <div>{actions}</div> : null}
          </div>
        </div>
        <main className="soa-page-content">{children}</main>
      </div>
    </div>
  );
}
