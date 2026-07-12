/** Deterministic API mocks (MSW) for web tests. */

import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";

export const DEFAULT_ME = {
  user_id: "u-1",
  email: "reviewer@northstar.example",
  display_name: "Riley Reviewer",
  auth_method: "dev",
  dev_session: true,
  memberships: [
    {
      membership_id: "m-1",
      organization_id: "org-1",
      organization_slug: "northstar",
      organization_name: "Northstar Distribution",
      organization_status: "active",
      status: "active",
      permissions: [
        "organization.read",
        "organization.manage",
        "documents.read",
        "documents.review",
        "processes.read",
        "streams.read",
        "catalogs.read",
        "integrations.read",
        "analytics.read",
      ],
    },
    {
      membership_id: "m-2",
      organization_id: "org-2",
      organization_slug: "meridian",
      organization_name: "Meridian Foods",
      organization_status: "active",
      status: "active",
      permissions: ["organization.read", "documents.read"],
    },
  ],
};

export const handlers = [http.get("/api/me", () => HttpResponse.json(DEFAULT_ME))];

export const server = setupServer(...handlers);
