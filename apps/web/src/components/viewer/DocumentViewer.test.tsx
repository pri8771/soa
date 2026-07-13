import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../../test/msw";
import { DocumentViewer } from "./DocumentViewer";

const FAILED_DOC = "84444444-4444-4444-8444-444444444444"; // has 2 rendered pages
const QUEUED_DOC = "81111111-1111-4111-8111-111111111111"; // no pages yet

function renderViewer(documentId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <DocumentViewer organizationSlug="northstar" documentId={documentId} />
    </QueryClientProvider>,
  );
}

async function makeLargeDocumentHandler(pageCount: number) {
  server.use(
    http.get(`/api/orgs/northstar/documents/${FAILED_DOC}/pages`, () =>
      HttpResponse.json({
        document_id: FAILED_DOC,
        run_id: "a1111111-1111-4111-8111-111111111111",
        run_number: 1,
        pages: Array.from({ length: pageCount }, (_, index) => ({
          page_number: index + 1,
          width_px: 1700,
          height_px: 2200,
          dpi: 200,
          rotation_degrees: 0,
          content_type: "image/png",
          image_artifact_id: `d${String(index).padStart(7, "0")}-0000-4000-8000-000000000000`,
          text_artifact_id: null,
        })),
      }),
    ),
  );
}

describe("Document viewer (REV-004)", () => {
  it("renders pages with navigation, zoom, and rotation controls", async () => {
    const user = userEvent.setup();
    renderViewer(FAILED_DOC);
    expect(await screen.findByText("Page 1 of 2")).toBeInTheDocument();

    // The current page resolves a SIGNED url — never an object key.
    const image = await screen.findByAltText("Page 1 of 2");
    expect(image).toHaveAttribute(
      "src",
      "https://storage.test/signed/c1111111-1111-4111-8111-111111111111",
    );

    await user.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("Page 2 of 2")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();

    // Zoom steps move the rendered width; rotation applies a transform.
    expect(screen.getByText("100%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByText("125%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rotate page" }));
    const rotated = await screen.findByAltText("Page 2 of 2");
    expect(rotated.style.transform).toBe("rotate(90deg)");
  });

  it("supports the documented keyboard map", async () => {
    const user = userEvent.setup();
    renderViewer(FAILED_DOC);
    const viewer = await screen.findByRole("group", { name: "Document viewer" });
    expect(viewer).toHaveAttribute("aria-description", expect.stringContaining("ArrowRight"));
    viewer.focus();
    await user.keyboard("{ArrowRight}");
    expect(await screen.findByText("Page 2 of 2")).toBeInTheDocument();
    await user.keyboard("{ArrowLeft}");
    expect(await screen.findByText("Page 1 of 2")).toBeInTheDocument();
    await user.keyboard("+");
    expect(screen.getByText("125%")).toBeInTheDocument();
    await user.keyboard("r");
    const image = await screen.findByAltText("Page 1 of 2");
    await waitFor(() => expect(image.style.transform).toBe("rotate(90deg)"));
  });

  it("thumbnails jump to their page and mark the current one", async () => {
    const user = userEvent.setup();
    renderViewer(FAILED_DOC);
    const thumbnails = await screen.findByRole("navigation", { name: "Pages" });
    const second = within(thumbnails).getByRole("button", { name: "Go to page 2" });
    await user.click(second);
    expect(await screen.findByText("Page 2 of 2")).toBeInTheDocument();
    expect(second).toHaveAttribute("aria-current", "page");
    // Thumbnails load lazily so offscreen pages are not fetched eagerly.
    const thumb = within(thumbnails).getByAltText("Page 1 thumbnail");
    expect(thumb).toHaveAttribute("loading", "lazy");
  });

  it("searches extracted text and jumps to matches", async () => {
    const user = userEvent.setup();
    renderViewer(FAILED_DOC);
    await screen.findByText("Page 1 of 2");
    await user.type(screen.getByLabelText("Search text"), "PO-100042");
    await user.click(screen.getByRole("button", { name: "Search" }));
    const results = await screen.findByRole("list", { name: "Search results" });
    // Only page 2 has a text artifact; it matches once.
    await user.click(within(results).getByRole("button", { name: "Page 2 (1)" }));
    expect(await screen.findByText("Page 2 of 2")).toBeInTheDocument();
  });

  it("says honestly when no text exists to search", async () => {
    const user = userEvent.setup();
    await makeLargeDocumentHandler(3); // pages without text artifacts
    renderViewer(FAILED_DOC);
    await screen.findByText("Page 1 of 3");
    await user.type(screen.getByLabelText("Search text"), "anything");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Text search isn’t available yet")).toBeInTheDocument();
  });

  it("handles a large document: all pages navigable, one full-size image mounted", async () => {
    const user = userEvent.setup();
    await makeLargeDocumentHandler(120);
    renderViewer(FAILED_DOC);
    expect(await screen.findByText("Page 1 of 120")).toBeInTheDocument();
    const thumbnails = screen.getByRole("navigation", { name: "Pages" });
    expect(within(thumbnails).getAllByRole("button")).toHaveLength(120);
    await user.click(within(thumbnails).getByRole("button", { name: "Go to page 120" }));
    expect(await screen.findByText("Page 120 of 120")).toBeInTheDocument();
    // Exactly ONE full-size page image is mounted (alt "Page N of 120").
    expect(screen.getAllByAltText(/^Page \d+ of 120$/)).toHaveLength(1);
  });

  it("renews an expired signed URL once on image error", async () => {
    let calls = 0;
    server.use(
      http.post("/api/orgs/northstar/artifacts/:artifactId/download-url", ({ params }) => {
        calls += 1;
        return HttpResponse.json({
          url: `https://storage.test/signed/${String(params["artifactId"])}?v=${calls}`,
          expires_at: "2026-07-13T12:05:00+00:00",
          method: "GET",
        });
      }),
    );
    renderViewer(FAILED_DOC);
    const image = await screen.findByAltText("Page 1 of 2");
    const before = image.getAttribute("src");
    image.dispatchEvent(new Event("error"));
    await waitFor(() => {
      const now = screen.getByAltText("Page 1 of 2").getAttribute("src");
      expect(now).not.toBe(before);
    });
  });

  it("is honest when there are no rendered pages", async () => {
    renderViewer(QUEUED_DOC);
    expect(await screen.findByText(/No rendered pages yet/)).toBeInTheDocument();
  });
});
