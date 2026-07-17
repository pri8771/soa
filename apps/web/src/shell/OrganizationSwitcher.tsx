/**
 * Organization switcher (TEN-012): deliberate, visually obvious switching.
 * Switching removes every cached query so no prior-tenant data survives,
 * then navigates into the selected organization.
 */

import { Button, Menu, MenuItem, MenuTrigger } from "@soa/design-system";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";

import { useShellSession } from "./ShellContext";

export function OrganizationSwitcher() {
  const session = useShellSession();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const others = (session.availableOrganizations ?? []).filter(
    (organization) => organization.slug !== session.organization.slug,
  );

  if (others.length === 0) {
    return (
      <span className="soa-shell-org" data-testid="current-organization">
        {session.organization.name}
      </span>
    );
  }

  return (
    <MenuTrigger>
      <Button variant="subtle" className="soa-shell-org" data-testid="current-organization">
        {session.organization.name}
        <span aria-hidden="true"> ▾</span>
      </Button>
      <Menu
        aria-label="Switch organization"
        onAction={(key) => {
          // Tenant isolation: drop every cached response before the switch
          // so nothing from the previous organization can flash.
          queryClient.removeQueries();
          void navigate({
            to: "/app/$organizationSlug/skills",
            params: { organizationSlug: String(key) },
          });
        }}
      >
        {others.map((organization) => (
          <MenuItem key={organization.slug} id={organization.slug}>
            {organization.name}
          </MenuItem>
        ))}
      </Menu>
    </MenuTrigger>
  );
}
