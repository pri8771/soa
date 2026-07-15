import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/settings";
const CREATED_API_KEY = ["soa", "abc12345", "visible-exactly-once"].join("_");
const ROTATED_API_KEY = ["soa", "def67890", "rotated"].join("_");

describe("organization settings", () => {
  it("shows members, roles, and data-export controls from real APIs", async () => {
    await renderApp(PATH);
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();
    const members = screen.getByRole("region", { name: "Members and roles" });
    expect(await within(members).findByText("reviewer@northstar.example")).toBeInTheDocument();
    expect(within(members).getAllByText("Organization admin").length).toBeGreaterThan(0);
    const exports = screen.getByRole("region", { name: "Organization data exports" });
    expect(
      within(exports).getByRole("button", { name: "Request organization export" }),
    ).toBeInTheDocument();
  });

  it("creates an invitation without inventing a role grant", async () => {
    const user = userEvent.setup();
    let invited: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/invitations", async ({ request }) => {
        invited = await request.json();
        return HttpResponse.json(
          { membership_id: "m-3", email: "pilot@example.com", status: "invited", created: true },
          { status: 201 },
        );
      }),
    );
    await renderApp(PATH);
    await user.type(await screen.findByLabelText("Invite by email"), "pilot@example.com");
    await user.click(screen.getByRole("button", { name: "Create invitation" }));

    await waitFor(() => expect(invited).toEqual({ email: "pilot@example.com" }));
    expect(await screen.findByText("Invitation created")).toBeInTheDocument();
    expect(screen.getByText(/does not send an email yet/i)).toBeInTheDocument();
  });

  it("reads back and revokes an assigned role", async () => {
    const user = userEvent.setup();
    let assigned = true;
    let revoked: string | null = null;
    server.use(
      http.get("/api/orgs/northstar/members", () =>
        HttpResponse.json({
          items: [
            {
              membership_id: "m-1",
              user_id: "u-1",
              invited_email: "reviewer@northstar.example",
              status: "active",
              version: 2,
              assigned_roles: assigned
                ? [
                    {
                      id: "role-admin",
                      name: "Organization admin",
                      slug: "org-admin",
                      is_system: true,
                    },
                  ]
                : [],
            },
          ],
          has_more: false,
          next_cursor: null,
        }),
      ),
      http.delete("/api/orgs/northstar/members/m-1/roles/:roleSlug", ({ params }) => {
        revoked = String(params["roleSlug"]);
        assigned = false;
        return HttpResponse.json({ membership_id: "m-1", role_slug: revoked, revoked: true });
      }),
    );
    await renderApp(PATH);
    await user.click(await screen.findByRole("button", { name: "Revoke Organization admin" }));
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Revoke role" }),
    );
    await waitFor(() => expect(revoked).toBe("org-admin"));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Revoke Organization admin" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("requests an idempotent durable organization export", async () => {
    const user = userEvent.setup();
    let idempotencyKey: string | null = null;
    server.use(
      http.post("/api/orgs/northstar/data-exports", ({ request }) => {
        idempotencyKey = request.headers.get("Idempotency-Key");
        return HttpResponse.json(
          {
            id: "export-new",
            scope: "organization",
            snapshot_at: "2026-07-15T12:00:00Z",
            state: "pending",
            total_documents: 0,
            processed_documents: 0,
            progress: 0,
            total_records: 0,
            safe_error: null,
            expires_at: "2026-07-16T12:00:00Z",
            manifest_download_url: null,
            manifest_expires_at: null,
            parts: [],
          },
          { status: 202 },
        );
      }),
    );
    await renderApp(PATH);
    await user.click(await screen.findByRole("button", { name: "Request organization export" }));
    await waitFor(() => expect(idempotencyKey).toMatch(/^[0-9a-f-]{36}$/));
  });

  it("creates a stream-scoped key and clears its one-time value on acknowledgement", async () => {
    const user = userEvent.setup();
    let submitted: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/service-credentials", async ({ request }) => {
        submitted = await request.json();
        return HttpResponse.json(
          {
            credential: {
              id: "credential-new",
              name: "Warehouse connector",
              key_prefix: "abc12345",
              scopes: ["documents.upload"],
              allowed_stream_ids: ["41111111-1111-4111-8111-111111111111"],
              status: "active",
              expires_at: "2026-10-13T12:00:00Z",
              last_used_at: null,
              created_by: "user:u-1",
              created_at: "2026-07-15T12:00:00Z",
              updated_at: "2026-07-15T12:00:00Z",
              version: 1,
            },
            api_key: CREATED_API_KEY,
            warning: "Copy this API key now. It cannot be retrieved after this response.",
          },
          { status: 201 },
        );
      }),
    );
    await renderApp(PATH);
    const section = await screen.findByRole("region", { name: "Service credentials" });
    await user.type(within(section).getByLabelText("Credential name"), "Warehouse connector");
    await user.click(await within(section).findByRole("checkbox", { name: /Email intake/ }));
    await user.click(within(section).getByRole("button", { name: "Create service credential" }));

    await waitFor(() =>
      expect(submitted).toEqual({
        name: "Warehouse connector",
        scopes: ["documents.upload"],
        allowed_stream_ids: ["41111111-1111-4111-8111-111111111111"],
        expires_in_days: 90,
      }),
    );
    expect(await within(section).findByText(CREATED_API_KEY)).toBeInTheDocument();
    await user.click(within(section).getByRole("button", { name: "I stored the key securely" }));
    expect(within(section).queryByText(CREATED_API_KEY)).not.toBeInTheDocument();
  });

  it("sends the displayed version as If-Match when rotating", async () => {
    const user = userEvent.setup();
    let ifMatch: string | null = null;
    server.use(
      http.get("/api/orgs/northstar/service-credentials", () =>
        HttpResponse.json({
          items: [
            {
              id: "credential-existing",
              name: "Warehouse connector",
              key_prefix: "abc12345",
              scopes: ["documents.upload"],
              allowed_stream_ids: ["41111111-1111-4111-8111-111111111111"],
              status: "active",
              expires_at: "2026-10-13T12:00:00Z",
              last_used_at: null,
              created_by: "user:u-1",
              created_at: "2026-07-15T12:00:00Z",
              updated_at: "2026-07-15T12:00:00Z",
              version: 7,
            },
          ],
        }),
      ),
      http.post(
        "/api/orgs/northstar/service-credentials/credential-existing/rotate",
        ({ request }) => {
          ifMatch = request.headers.get("If-Match");
          return HttpResponse.json({
            credential: {
              id: "credential-existing",
              name: "Warehouse connector",
              key_prefix: "def67890",
              scopes: ["documents.upload"],
              allowed_stream_ids: ["41111111-1111-4111-8111-111111111111"],
              status: "active",
              expires_at: "2026-10-13T12:00:00Z",
              last_used_at: null,
              created_by: "user:u-1",
              created_at: "2026-07-15T12:00:00Z",
              updated_at: "2026-07-15T13:00:00Z",
              version: 8,
            },
            api_key: ROTATED_API_KEY,
            warning: "Copy this API key now.",
          });
        },
      ),
    );
    await renderApp(PATH);
    await user.click(await screen.findByRole("button", { name: "Rotate Warehouse connector" }));
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Rotate key" }),
    );
    await waitFor(() => expect(ifMatch).toBe("7"));
    expect(await screen.findByText(ROTATED_API_KEY)).toBeInTheDocument();
  });
});
