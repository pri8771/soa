import { HttpResponse, delay, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/documents/upload";

function pdfFile(name = "po.pdf", content = "%PDF-1.7 test"): File {
  return new File([content], name, { type: "application/pdf" });
}

async function pickFiles(user: ReturnType<typeof userEvent.setup>, ...files: File[]) {
  const input = screen.getByLabelText("Choose files to upload");
  await user.upload(input as HTMLInputElement, files);
}

describe("Upload documents (ING-008)", () => {
  it("uploads a valid file end to end and reports Queued", async () => {
    const user = userEvent.setup({ applyAccept: false });
    let putBody: ArrayBuffer | null = null;
    server.use(
      http.put("https://storage.test/*", async ({ request }) => {
        putBody = await request.arrayBuffer();
        return new HttpResponse(null, { status: 200 });
      }),
    );
    await renderApp(PATH);
    await screen.findByRole("button", { name: "Choose files" });
    await pickFiles(user, pdfFile());
    expect(screen.getByText("po.pdf")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Upload 1 file/ }));
    expect(await screen.findByText("Queued")).toBeInTheDocument();
    expect(screen.getByText("Queued for processing.")).toBeInTheDocument();
    await waitFor(() => expect(putBody).not.toBeNull());
    expect(new TextDecoder().decode(putBody!)).toContain("%PDF");
  });

  it("keeps outcomes distinct: rejected, quarantined, duplicate", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const outcomes = [
      {
        state: "rejected",
        state_reason: "content is image/png but was declared as application/pdf",
        duplicate_of: null,
      },
      { state: "quarantined", state_reason: "malware detected: Win.Test", duplicate_of: null },
      { state: "queued", state_reason: null, duplicate_of: "70000000-0000-4000-8000-000000000009" },
    ];
    let call = 0;
    server.use(
      http.post("/api/orgs/northstar/uploads/:sessionId/complete", () =>
        HttpResponse.json({
          document_id: `7000000${call}-0000-4000-8000-00000000000${call}`,
          ...outcomes[call++ % outcomes.length],
        }),
      ),
    );
    await renderApp(PATH);
    await screen.findByRole("button", { name: "Choose files" });
    await pickFiles(user, pdfFile("a.pdf"), pdfFile("b.pdf"), pdfFile("c.pdf"));
    await user.click(screen.getByRole("button", { name: /Upload 3 files/ }));

    expect(await screen.findByText("Rejected")).toBeInTheDocument();
    expect(await screen.findByText("Quarantined")).toBeInTheDocument();
    expect(await screen.findByText("Duplicate")).toBeInTheDocument();
    // Reasons ride along.
    expect(screen.getByText(/declared as application\/pdf/)).toBeInTheDocument();
    expect(screen.getByText(/malware detected/)).toBeInTheDocument();
    expect(screen.getByText(/flagged for review/)).toBeInTheDocument();
    // Partial-result summary is announced.
    expect(screen.getByText(/3 processed · 2 need attention/)).toBeInTheDocument();
  });

  it("validates type and size before any network call", async () => {
    const user = userEvent.setup({ applyAccept: false });
    const sessionCalls: string[] = [];
    server.use(
      http.post("/api/orgs/northstar/streams/:streamSlug/uploads", ({ params }) => {
        sessionCalls.push(String(params["streamSlug"]));
        return HttpResponse.json({}, { status: 500 });
      }),
    );
    await renderApp(PATH);
    await screen.findByRole("button", { name: "Choose files" });
    const exe = new File(["MZ"], "malware.exe", { type: "application/x-msdownload" });
    await pickFiles(user, exe);
    expect(await screen.findByText("Not uploadable")).toBeInTheDocument();
    expect(screen.getByText(/Unsupported type/)).toBeInTheDocument();
    // Nothing ready to upload; the upload button is disabled and no
    // session was ever requested.
    expect(screen.getByRole("button", { name: /Upload/ })).toBeDisabled();
    expect(sessionCalls).toEqual([]);
  });

  it("cancelling an in-flight file aborts its session", async () => {
    const user = userEvent.setup({ applyAccept: false });
    let aborted = 0;
    server.use(
      http.put("https://storage.test/*", async () => {
        await delay(400); // keep the file in-flight long enough to cancel
        return new HttpResponse(null, { status: 200 });
      }),
      http.post("/api/orgs/northstar/uploads/:sessionId/abort", () => {
        aborted += 1;
        return HttpResponse.json({ state: "aborted" });
      }),
    );
    await renderApp(PATH);
    await screen.findByRole("button", { name: "Choose files" });
    await pickFiles(user, pdfFile("slow.pdf"));
    await user.click(screen.getByRole("button", { name: /Upload 1 file/ }));

    await user.click(await screen.findByRole("button", { name: "Cancel slow.pdf" }));
    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
    await waitFor(() => expect(aborted).toBe(1));
    // The cancelled outcome sticks even after the slow PUT resolves.
    await delay(500);
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
    expect(screen.queryByText("Queued")).not.toBeInTheDocument();
  });

  it("removes a file before upload without touching the network", async () => {
    const user = userEvent.setup({ applyAccept: false });
    await renderApp(PATH);
    await screen.findByRole("button", { name: "Choose files" });
    await pickFiles(user, pdfFile("keep.pdf"), pdfFile("drop.pdf"));
    await user.click(screen.getByRole("button", { name: "Remove drop.pdf" }));
    expect(screen.queryByText("drop.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("keep.pdf")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Upload 1 file/ })).toBeEnabled();
  });
});
