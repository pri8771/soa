/**
 * Canonical /api/me test payload, shared by the unit-test MSW handlers and
 * the Playwright e2e mocks so both suites exercise the same session shape.
 */

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
        "documents.reprocess",
        "documents.upload",
        "processes.read",
        "processes.manage",
        "streams.read",
        "streams.manage",
        "catalogs.read",
        "integrations.read",
        "analytics.read",
        "jobs.read",
        "jobs.manage",
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
