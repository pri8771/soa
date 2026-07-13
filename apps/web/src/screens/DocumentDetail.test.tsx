import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_DOCUMENTS, server } from "../test/msw";
import { renderApp } from "../test/render";

const QUEUED = DEFAULT_DOCUMENTS[0];
const QUARANTINED = DEFAULT_DOCUMENTS[2];

describe("Document detail (ING-012)", () => {
  it("is a stable deep link with summary, files, honest placeholders, and timeline", async () => {
    await renderApp(`/app/northstar/documents/${QUEUED.id}`);
    expect(await screen.findByRole("heading", { name: "po-4711.pdf" })).toBeInTheDocument();

    const summary = screen.getByRole("region", { name: "Summary" });
    expect(within(summary).getByText("queued")).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: "Email intake" })).toBeInTheDocument();
    expect(within(summary).getByText(/configuration v2/)).toBeInTheDocument();

    const files = screen.getByRole("region", { name: "Files" });
    expect(within(files).getByText("original")).toBeInTheDocument();
    expect(within(files).getByRole("button", { name: "Download original" })).toBeInTheDocument();

    // Unbuilt panels are honest, never fake data.
    expect(screen.getByText(/extraction arrives with processing/)).toBeInTheDocument();
    expect(screen.getByText(/exports arrive with EXP/)).toBeInTheDocument();

    const timeline = screen.getByRole("region", { name: "Timeline" });
    expect(within(timeline).getByText("document.received")).toBeInTheDocument();
    expect(within(timeline).getByText("received → validating file")).toBeInTheDocument();
  });

  it("offers cancel while the document is active", async () => {
    await renderApp(`/app/northstar/documents/${QUEUED.id}`);
    await screen.findByRole("heading", { name: "po-4711.pdf" });
    expect(screen.getByRole("button", { name: "Cancel document" })).toBeInTheDocument();
  });

  it("offers no cancel once the document is settled", async () => {
    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    await screen.findByRole("heading", { name: "invoice-evil.pdf" });
    expect(screen.queryByRole("button", { name: "Cancel document" })).not.toBeInTheDocument();
  });

  it("cancelling requires a reason and posts it", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    server.use(
      http.post("/api/orgs/northstar/documents/:documentId/cancel", async ({ request }) => {
        posted = await request.json();
        return HttpResponse.json({ id: QUEUED.id, state: "cancelled" });
      }),
    );
    await renderApp(`/app/northstar/documents/${QUEUED.id}`);
    await screen.findByRole("heading", { name: "po-4711.pdf" });
    await user.click(screen.getByRole("button", { name: "Cancel document" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Cancel document" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/Reason/), "wrong stream");
    await user.click(confirm);
    await waitFor(() => expect(posted).toEqual({ reason: "wrong stream" }));
  });

  it("surfaces a refused download (quarantined) without opening anything", async () => {
    const user = userEvent.setup();
    const opened: string[] = [];
    vi.spyOn(window, "open").mockImplementation((url) => {
      opened.push(String(url));
      return null;
    });
    server.use(
      http.post("/api/orgs/northstar/artifacts/:artifactId/download-url", () =>
        HttpResponse.json(
          { error: { message: "This document is quarantined; its files cannot be downloaded." } },
          { status: 403 },
        ),
      ),
    );
    await renderApp(`/app/northstar/documents/${QUARANTINED.id}`);
    await screen.findByRole("heading", { name: "invoice-evil.pdf" });
    await user.click(screen.getByRole("button", { name: "Download original" }));
    expect(await screen.findByText("Download refused")).toBeInTheDocument();
    expect(screen.getByText(/quarantined; its files cannot/)).toBeInTheDocument();
    expect(opened).toEqual([]);
  });
});
