import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_DELETION_REQUEST, DEFAULT_DOCUMENTS, DEFAULT_ME, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/documents";

describe("Documents queue (ING-010)", () => {
  it("shows pending deletion approvals with document, requester, and age", async () => {
    const requestedAt = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    server.use(
      http.get("/api/orgs/northstar/deletion-requests", () =>
        HttpResponse.json({
          items: [
            { ...DEFAULT_DELETION_REQUEST, requested_by: "u-2", requested_at: requestedAt },
            {
              ...DEFAULT_DELETION_REQUEST,
              id: "d8888888-8888-4888-8888-888888888888",
              state: "cancelled",
            },
          ],
        }),
      ),
    );

    await renderApp(PATH);
    const approvals = await screen.findByRole("region", { name: "Deletion approvals" });
    const documentLink = await within(approvals).findByRole("link", {
      name: `Document ${DEFAULT_DELETION_REQUEST.document_id}`,
    });
    expect(documentLink).toHaveAttribute(
      "href",
      expect.stringContaining(`/documents/${DEFAULT_DELETION_REQUEST.document_id}`),
    );
    expect(within(approvals).getByText(/requested by u-2 · 2h ago/)).toBeInTheDocument();
    expect(within(approvals).getAllByRole("listitem")).toHaveLength(1);
  });

  it("does not query or render deletion approvals without approval permission", async () => {
    let requests = 0;
    server.use(
      http.get("/api/me", () =>
        HttpResponse.json({
          ...DEFAULT_ME,
          memberships: [
            {
              ...DEFAULT_ME.memberships[0],
              permissions: DEFAULT_ME.memberships[0].permissions.filter(
                (permission) => permission !== "data.delete.approve",
              ),
            },
          ],
        }),
      ),
      http.get("/api/orgs/northstar/deletion-requests", () => {
        requests += 1;
        return HttpResponse.json({ items: [DEFAULT_DELETION_REQUEST] });
      }),
    );

    await renderApp(PATH);
    await screen.findByText("po-4711.pdf");
    expect(screen.queryByRole("region", { name: "Deletion approvals" })).not.toBeInTheDocument();
    expect(requests).toBe(0);
  });

  it("warns when approver discovery reaches the latest-200 cap", async () => {
    server.use(
      http.get("/api/orgs/northstar/deletion-requests", () =>
        HttpResponse.json({
          items: Array.from({ length: 200 }, (_, index) => ({
            ...DEFAULT_DELETION_REQUEST,
            id: `request-${index}`,
            document_id: `document-${index}`,
          })),
        }),
      ),
    );

    await renderApp(PATH);
    const approvals = await screen.findByRole("region", { name: "Deletion approvals" });
    expect(
      await within(approvals).findByText("Approval list may be incomplete"),
    ).toBeInTheDocument();
    expect(within(approvals).getByText(/latest 200 deletion requests/)).toBeInTheDocument();
  });

  it("lists documents with distinct states, duplicate flags, and reasons", async () => {
    await renderApp(PATH);
    expect(await screen.findByText("po-4711.pdf")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Documents" });
    // Stream name resolved, not the raw id. The deleted fixture is a
    // tombstone, not active work — the default view hides it (see the
    // dedicated "deleted" filter test below).
    expect(within(table).getAllByText("Email intake").length).toBe(4);
    // Distinct outcomes: queued, queued+duplicate flag, quarantined+reason.
    expect(within(table).getAllByText("queued").length).toBe(2);
    expect(within(table).getByText("duplicate")).toBeInTheDocument();
    expect(within(table).getByText("quarantined")).toBeInTheDocument();
    expect(within(table).getByText(/malware detected/)).toBeInTheDocument();
    expect(within(table).queryByText("deleted")).not.toBeInTheDocument();
    // Upload entry point.
    expect(screen.getByRole("link", { name: "Upload documents" })).toBeInTheDocument();
  });

  it("persists the state filter in the URL and narrows the list", async () => {
    const user = userEvent.setup();
    const { router } = await renderApp(PATH);
    await screen.findByText("po-4711.pdf");
    await user.click(screen.getByRole("button", { name: /State/ }));
    await user.click(await screen.findByRole("option", { name: "Quarantined" }));

    await waitFor(() => expect(screen.queryByText("po-4711.pdf")).not.toBeInTheDocument());
    expect(screen.getByText("invoice-evil.pdf")).toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ docState: "quarantined" });
  });

  it("searches by filename", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByText("po-4711.pdf");
    await user.type(screen.getByLabelText("Search filename"), "4712");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(screen.queryByText("po-4711.pdf")).not.toBeInTheDocument());
    expect(screen.getByText("po-4712.pdf")).toBeInTheDocument();
  });

  it("offers bulk cancel only while every selected document is cancellable", async () => {
    const user = userEvent.setup();
    const cancelled: string[] = [];
    server.use(
      http.post("/api/orgs/northstar/documents/:documentId/cancel", ({ params }) => {
        cancelled.push(String(params["documentId"]));
        return HttpResponse.json({ id: String(params["documentId"]), state: "cancelled" });
      }),
    );
    await renderApp(PATH);
    await screen.findByText("po-4711.pdf");

    // Select a queued (cancellable) document: action enabled.
    await user.click(
      screen.getByRole("checkbox", { name: `Select row ${DEFAULT_DOCUMENTS[0].id}` }),
    );
    const cancelButton = await screen.findByRole("button", { name: "Cancel selected" });
    expect(cancelButton).toBeEnabled();

    // Add the quarantined (settled) document: action disabled + explained.
    const quarantinedBox = screen.getByRole("checkbox", {
      name: `Select row ${DEFAULT_DOCUMENTS[2].id}`,
    });
    await user.click(quarantinedBox);
    expect(screen.getByRole("button", { name: "Cancel selected" })).toBeDisabled();
    expect(screen.getByText(/already settled/)).toBeInTheDocument();

    // Back to a valid selection: cancel fires per document.
    await user.click(quarantinedBox);
    await user.click(screen.getByRole("button", { name: "Cancel selected" }));
    await waitFor(() => expect(cancelled).toEqual([DEFAULT_DOCUMENTS[0].id]));
  });

  it("filters to deleted documents", async () => {
    const user = userEvent.setup();
    const { router } = await renderApp(PATH);
    await screen.findByText("po-4711.pdf");
    await user.click(screen.getByRole("button", { name: /State/ }));
    await user.click(await screen.findByRole("option", { name: "Deleted" }));

    await waitFor(() => expect(screen.queryByText("po-4711.pdf")).not.toBeInTheDocument());
    expect(screen.getByText("[deleted]")).toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ docState: "deleted" });
  });
});
