import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderApp as renderAppAtPath } from "../test/render";

async function renderApp(initialPath = "/app/northstar/overview") {
  const { router } = await renderAppAtPath(initialPath);
  await screen.findByRole("navigation", { name: "Primary" });
  return router;
}

describe("CommandPalette", () => {
  it("advertises the shortcut on the trigger", async () => {
    await renderApp();
    const trigger = screen.getByRole("button", { name: /Search or jump to/ });
    expect(trigger).toHaveTextContent("Ctrl K");
  });

  it("opens with Ctrl+K, filters commands, and navigates on Enter", async () => {
    const user = userEvent.setup();
    const router = await renderApp();

    await user.keyboard("{Control>}k{/Control}");
    const input = await screen.findByRole("combobox", { name: "Search commands" });
    expect(input).toHaveFocus();

    // All nav commands visible for the full-permission dev session.
    expect(screen.getAllByRole("option").length).toBeGreaterThanOrEqual(9);

    await user.keyboard("docum");
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(1);
    expect(options[0]).toHaveTextContent("Go to Documents");

    await user.keyboard("{Enter}");
    await waitFor(() => expect(router.state.location.pathname).toBe("/app/northstar/documents"));
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("supports arrow-key selection and Escape close", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox", { name: "Search commands" });

    await user.keyboard("{ArrowDown}{ArrowDown}");
    const active = screen
      .getAllByRole("option")
      .find((option) => option.getAttribute("aria-selected") === "true");
    expect(active).toHaveTextContent("Go to Overview");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("shows a no-results state with guidance", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.keyboard("{Control>}k{/Control}");
    await screen.findByRole("combobox", { name: "Search commands" });
    await user.keyboard("zzzznope");
    expect(screen.getByText(/No matching commands/)).toBeInTheDocument();
  });
});

describe("CommandPalette permissions", () => {
  it("only surfaces commands the session permits", async () => {
    // The permission-filtering logic is shared with the nav; verify at the
    // command-builder level with a restricted session.
    const { buildNavigationCommands } = await import("./commands");
    const session = {
      organization: { slug: "northstar", name: "Northstar" },
      permissions: new Set(["documents.read", "organization.read"]),
      devSession: true,
      userLabel: "test",
      userId: "u-1",
    };
    const commands = buildNavigationCommands(session, () => {});
    expect(commands.map((command) => command.label)).toEqual([
      "Go to Getting started",
      "Go to Overview",
      "Go to Documents",
      "Go to Support",
    ]);
  });
});
