/**
 * Application shell (DSN-004, UI_UX_BLUEPRINT §3).
 *
 * Left navigation (248px expanded / 72px collapsed, persisted), 56px top
 * context bar with an always-visible organization display, page header with
 * breadcrumbs, and permission-aware navigation: items the principal cannot
 * use are not rendered at all.
 */

import { Badge, Button } from "@soa/design-system";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useState, type ReactNode } from "react";

import { useAuth } from "../auth/AuthContext";
import { BREAKPOINT_COMPACT_LAYOUT, BREAKPOINT_COMPACT_NAV } from "./breakpoints";
import { CommandPalette } from "./CommandPalette";
import { OrganizationSwitcher } from "./OrganizationSwitcher";
import { useShellSession } from "./ShellContext";
import "./shell.css";

/** Primary navigation, in blueprint order (§3.1). */
export const NAV_ITEMS = [
  {
    label: "Getting started",
    to: "/app/$organizationSlug/getting-started",
    permission: "organization.read",
    icon: "✦",
  },
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
  {
    label: "Support",
    to: "/app/$organizationSlug/support",
    permission: "organization.read",
    icon: "☎",
  },
] as const;

export type NavItem = (typeof NAV_ITEMS)[number];

const NAV_COLLAPSE_KEY = "soa.nav.collapsed";
const COMPACT_NAV_QUERY = `(max-width: ${BREAKPOINT_COMPACT_NAV}px)`;
const NARROW_NAV_QUERY = `(max-width: ${BREAKPOINT_COMPACT_LAYOUT}px)`;

function readCollapsePreference(): boolean | null {
  try {
    const stored = globalThis.localStorage?.getItem(NAV_COLLAPSE_KEY);
    return stored === null || stored === undefined ? null : stored === "true";
  } catch {
    // Storage is optional (for example, browsers may block it in private
    // contexts). The viewport-derived default remains fully functional.
    return null;
  }
}

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() =>
    typeof globalThis.matchMedia === "function" ? globalThis.matchMedia(query).matches : false,
  );

  useEffect(() => {
    if (typeof globalThis.matchMedia !== "function") return;
    const media = globalThis.matchMedia(query);
    const update = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);

  return matches;
}

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
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [collapsePreference, setCollapsePreference] = useState<boolean | null>(
    readCollapsePreference,
  );
  const compactViewport = useMediaQuery(COMPACT_NAV_QUERY);
  const narrowViewport = useMediaQuery(NARROW_NAV_QUERY);

  // A persisted preference wins on desktop and compact desktop. Truly
  // narrow layouts always keep the rail collapsed so a desktop "expanded"
  // preference cannot consume most of a tablet or split-screen viewport.
  const collapsed = narrowViewport || (collapsePreference ?? compactViewport);

  //: Persist only explicit toggles — viewport changes never become a sticky
  //: preference. The control is omitted while the narrow safety override is
  //: active, so it never advertises an expansion the layout cannot honor.
  const toggleCollapsed = () => {
    const next = !collapsed;
    setCollapsePreference(next);
    try {
      globalThis.localStorage?.setItem(NAV_COLLAPSE_KEY, String(next));
    } catch {
      // Preference persistence is best effort; local state still updates.
    }
  };

  const visibleItems = NAV_ITEMS.filter((item) => session.permissions.has(item.permission));
  const signOut = async () => {
    queryClient.removeQueries();
    await auth.logout();
  };

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
        {!narrowViewport ? (
          <div className="soa-shell-nav-footer">
            <Button variant="subtle" size="sm" onPress={toggleCollapsed}>
              <span aria-hidden="true">{collapsed ? "»" : "«"}</span>
              <span className="soa-shell-nav-label">
                {collapsed ? "Expand navigation" : "Collapse navigation"}
              </span>
            </Button>
          </div>
        ) : null}
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
        <span className="soa-shell-user">{session.userLabel}</span>
        {!session.devSession ? (
          <Button variant="subtle" size="sm" onPress={() => void signOut()}>
            Sign out
          </Button>
        ) : null}
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
