import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_ME, server } from "../test/msw";
import { renderApp } from "../test/render";

describe("Streams list (CFG-010)", () => {
  it("lists streams with parent process and published version", async () => {
    await renderApp("/app/northstar/streams");
    expect(await screen.findByRole("link", { name: "Email intake" })).toBeInTheDocument();
    expect(screen.getByText("Purchase orders")).toBeInTheDocument();
    expect(screen.getByText("v2")).toBeInTheDocument();
  });
});

describe("Stream detail (CFG-010)", () => {
  it("keeps hierarchy visible and shows the published configuration", async () => {
    const { router } = await renderApp("/app/northstar/streams/email");
    expect(await screen.findByRole("heading", { name: "Email intake" })).toBeInTheDocument();
    // Hierarchy: breadcrumb back to Streams + parent process named.
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toHaveTextContent("Streams");
    expect(await screen.findByText("Purchase orders")).toBeInTheDocument();
    // Resolved configuration values from the published snapshot.
    expect(screen.getByText("confidence_floor")).toBeInTheDocument();
    expect(screen.getByText("0.95")).toBeInTheDocument();
    // Implemented intake surfaces replace the former placeholder.
    expect(
      screen.getByRole("link", { name: "Upload documents in the browser" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/POST \/v1\/streams\/email\/documents/)).toBeInTheDocument();
    // Version history with pin context.
    expect(screen.getAllByText(/pins process v3/).length).toBe(2);
    await userEvent
      .setup()
      .click(screen.getByRole("link", { name: "Upload documents in the browser" }));
    await waitFor(() =>
      expect((router.state.location.search as { stream?: string }).stream).toBe("email"),
    );
  });

  it("archive requires an impact explanation before it is enabled", async () => {
    const user = userEvent.setup();
    let archiveBody: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/streams/email/archive", async ({ request }) => {
        archiveBody = await request.json();
        return HttpResponse.json({});
      }),
    );
    await renderApp("/app/northstar/streams/email");
    await screen.findByRole("heading", { name: "Email intake" });

    await user.click(screen.getByRole("button", { name: "Archive stream" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Archive stream" });
    expect(confirm).toBeDisabled();

    await user.type(
      within(dialog).getByLabelText(/Impact of archiving/),
      "Supplier moved to the API stream.",
    );
    await user.click(confirm);
    await waitFor(() =>
      expect(archiveBody).toEqual({ impact: "Supplier moved to the API stream." }),
    );
  });

  it("hides the archive control without streams.manage", async () => {
    server.use(
      http.get("/api/me", () =>
        HttpResponse.json({
          ...DEFAULT_ME,
          memberships: [
            {
              ...DEFAULT_ME.memberships[0],
              permissions: ["organization.read", "streams.read"],
            },
          ],
        }),
      ),
    );
    await renderApp("/app/northstar/streams/email");
    await screen.findByRole("heading", { name: "Email intake" });
    expect(screen.queryByRole("button", { name: "Archive stream" })).not.toBeInTheDocument();
  });

  it("shows a retryable error state when the stream fails to load", async () => {
    server.use(
      http.get("/api/orgs/:slug/streams/:streamSlug", () => HttpResponse.json({}, { status: 500 })),
    );
    await renderApp("/app/northstar/streams/email");
    expect(await screen.findByText("Couldn’t load this stream")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
