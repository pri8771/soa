import { HttpResponse, http } from "msw";
import { focusManager } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_ME, server } from "../test/msw";
import { renderApp } from "../test/render";

afterEach(() => {
  focusManager.setFocused(undefined);
});

describe("AppLayout session wiring (TEN-012)", () => {
  it("builds the shell from /me: org name, permissions, dev badge", async () => {
    await renderApp("/app/northstar/overview");
    expect(await screen.findByTestId("current-organization")).toHaveTextContent(
      "Northstar Distribution",
    );
    expect(screen.getByText("Development identity")).toBeInTheDocument();
    expect(screen.getByText("reviewer@northstar.example")).toBeInTheDocument();
    // Permission-aware nav: no integrations.manage etc., but documents.read
    expect(screen.getByRole("link", { name: /Documents/ })).toBeInTheDocument();
  });

  it("shows the unauthorized state for organizations without membership", async () => {
    await renderApp("/app/not-my-org/overview");
    expect(
      await screen.findByText("You don’t have access to this organization"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Choose an organization" })).toBeInTheDocument();
  });

  it("shows the suspended state for non-operational organizations", async () => {
    server.use(
      http.get("/api/me", () =>
        HttpResponse.json({
          ...DEFAULT_ME,
          memberships: [
            {
              ...DEFAULT_ME.memberships[0],
              organization_status: "suspended",
            },
          ],
        }),
      ),
    );
    await renderApp("/app/northstar/overview");
    expect(await screen.findByText("This organization is suspended")).toBeInTheDocument();
  });

  it("shows a retryable error state when /me fails", async () => {
    server.use(http.get("/api/me", () => HttpResponse.json({}, { status: 500 })));
    await renderApp("/app/northstar/overview");
    expect(await screen.findByText("Couldn’t load your session")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("refreshes fresh authorization context on focus and removes revoked navigation", async () => {
    let response = DEFAULT_ME;
    let requestCount = 0;
    server.use(
      http.get("/api/me", () => {
        requestCount += 1;
        return HttpResponse.json(response);
      }),
    );

    await renderApp("/app/northstar/overview");
    const primary = await screen.findByRole("navigation", { name: "Primary" });
    expect(within(primary).getByRole("link", { name: /Review/ })).toBeInTheDocument();
    expect(requestCount).toBe(1);

    // The cached /me response is still inside its 30-second stale window.
    // Focus must nevertheless fetch the new effective permissions.
    response = {
      ...DEFAULT_ME,
      memberships: [
        {
          ...DEFAULT_ME.memberships[0],
          permissions: ["organization.read"],
        },
      ],
    };
    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });

    await waitFor(() => expect(requestCount).toBe(2));
    await waitFor(() =>
      expect(within(primary).queryByRole("link", { name: /Review/ })).not.toBeInTheDocument(),
    );
  });
});

describe("Organization switching (TEN-012)", () => {
  it("switches org, clears cached queries, and shows no stale tenant data", async () => {
    const user = userEvent.setup();
    const { router, queryClient } = await renderApp("/app/northstar/overview");
    await screen.findByTestId("current-organization");

    const removeSpy = vi.spyOn(queryClient, "removeQueries");
    await user.click(screen.getByTestId("current-organization"));
    await user.click(await screen.findByRole("menuitem", { name: "Meridian Foods" }));

    await waitFor(() => expect(router.state.location.pathname).toBe("/app/meridian/overview"));
    expect(removeSpy).toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.getByTestId("current-organization")).toHaveTextContent("Meridian Foods"),
    );
    // The restricted org lacks documents.review: Review nav must not linger.
    // (Scoped to the primary nav — the ANA-004 dashboard body has its
    // own review drill-down links.)
    const nav = screen.getByRole("navigation", { name: "Primary" });
    expect(within(nav).queryByRole("link", { name: /Review/ })).not.toBeInTheDocument();
  });
});

describe("SelectOrganization screen", () => {
  it("lists active organizations with open links", async () => {
    await renderApp("/select-organization");
    expect(
      await screen.findByRole("heading", { name: "Choose an organization" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Northstar Distribution")).toBeInTheDocument();
    expect(screen.getByText("Meridian Foods")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Open" })).toHaveLength(2);
  });

  it("marks suspended organizations instead of linking into them", async () => {
    server.use(
      http.get("/api/me", () =>
        HttpResponse.json({
          ...DEFAULT_ME,
          memberships: [
            DEFAULT_ME.memberships[0],
            { ...DEFAULT_ME.memberships[1], organization_status: "suspended" },
          ],
        }),
      ),
    );
    await renderApp("/select-organization");
    await screen.findByText("Northstar Distribution");
    expect(screen.getAllByRole("link", { name: "Open" })).toHaveLength(1);
    expect(screen.getByText("suspended")).toBeInTheDocument();
  });
});
