import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

// Replace the pixel-drawing viewer with a stub that exposes the current
// drawTarget and a button that fires onRegionDrawn — so the draw → assign →
// autofill wiring is exercised deterministically without a real canvas.
vi.mock("../components/viewer/DocumentViewer", () => ({
  DocumentViewer: ({
    drawTarget,
    onRegionDrawn,
  }: {
    drawTarget: string | null;
    onRegionDrawn?: (region: { pageNumber: number; polygon: number[][] }) => void;
  }) => (
    <div>
      <span data-testid="draw-target">{drawTarget ?? "none"}</span>
      <button
        type="button"
        onClick={() =>
          onRegionDrawn?.({
            pageNumber: 1,
            polygon: [
              [10, 10],
              [90, 10],
              [90, 30],
              [10, 30],
            ],
          })
        }
      >
        simulate draw
      </button>
    </div>
  ),
}));

const SCHEMA = {
  versions: [
    {
      id: "sv-1",
      version_number: 1,
      state: "published",
      version: 1,
      change_summary: null,
      definition: {
        fields: [
          { key: "po_number", label: "PO number", type: "text", criticality: "critical" },
          {
            key: "lines",
            label: "Lines",
            type: "table",
            columns: [
              { key: "sku", label: "SKU", type: "text" },
              { key: "quantity", label: "Qty", type: "number" },
            ],
          },
        ],
      },
    },
  ],
  published_json_schema: { type: "object" },
};

function setup(captured: { body?: { ground_truth: Record<string, unknown> } }) {
  server.use(
    http.get("/api/orgs/:slug/processes/:processSlug/schema", () => HttpResponse.json(SCHEMA)),
    http.get("/api/orgs/:slug/streams/:stream/training-sets/:ts", () =>
      HttpResponse.json({
        id: "ts-1",
        slug: "my-set",
        name: "My set",
        description: null,
        stream_id: "41111111-1111-4111-8111-111111111111",
        privacy_classification: "customer_confidential",
        working_draft_version_id: "v-1",
        published_version_id: null,
        document_count: 0,
        versions: [
          {
            id: "v-1",
            version_number: 1,
            state: "draft",
            published_at: null,
            counts: { train: 0, validation: 0, test: 0, total: 0 },
          },
        ],
        documents: [],
      }),
    ),
    http.post("/api/orgs/:slug/documents/:documentId/text-in-region", () =>
      HttpResponse.json({ text: "PO-4711" }),
    ),
    http.put("/api/orgs/:slug/streams/:stream/training-sets/:ts/documents", async ({ request }) => {
      captured.body = (await request.json()) as { ground_truth: Record<string, unknown> };
      return HttpResponse.json({
        id: "gd-1",
        dataset_version_id: "v-1",
        source_document_id: "doc-1",
        document_sha256: "a".repeat(64),
        split: "train",
        expected_class: null,
        ground_truth: captured.body.ground_truth,
      });
    }),
  );
}

const ROUTE = "/app/northstar/streams/email/training/my-set/documents/doc-1";

describe("Training annotation (draw + label incl. line items)", () => {
  it("a drawn box fills the value from enclosed text and saves value + region", async () => {
    const captured: { body?: { ground_truth: Record<string, unknown> } } = {};
    setup(captured);
    const user = userEvent.setup();
    await renderApp(ROUTE);

    // Fields come from the published schema.
    const poField = await screen.findByRole("button", { name: /PO number/ });

    // Select the field → it becomes the draw target → drawing fills the value.
    await user.click(poField);
    expect(screen.getByTestId("draw-target")).toHaveTextContent("po_number");
    await user.click(screen.getByRole("button", { name: "simulate draw" }));
    await waitFor(() =>
      expect((screen.getAllByPlaceholderText("value")[0] as HTMLInputElement).value).toBe(
        "PO-4711",
      ),
    );

    await user.click(screen.getByRole("button", { name: "Save labels" }));
    await waitFor(() => expect(captured.body).toBeTruthy());
    const gt = captured.body!.ground_truth as {
      fields: Record<string, string | null>;
      regions: Record<string, { page_number: number }>;
    };
    expect(gt.fields.po_number).toBe("PO-4711");
    expect(gt.regions.po_number.page_number).toBe(1);
  });

  it("labels a line-item cell and saves it under lines with its region", async () => {
    const captured: { body?: { ground_truth: Record<string, unknown> } } = {};
    setup(captured);
    const user = userEvent.setup();
    await renderApp(ROUTE);
    await screen.findByRole("button", { name: /PO number/ });

    await user.click(screen.getByRole("button", { name: "Add row" }));
    // The row's SKU cell selector appears; select and draw it.
    const skuSelector = await screen.findByRole("button", { name: /SKU/ });
    await user.click(skuSelector);
    expect(screen.getByTestId("draw-target")).toHaveTextContent("lines.0.sku");
    await user.click(screen.getByRole("button", { name: "simulate draw" }));

    await user.click(screen.getByRole("button", { name: "Save labels" }));
    await waitFor(() => expect(captured.body).toBeTruthy());
    const gt = captured.body!.ground_truth as {
      lines?: Record<string, string | null>[];
      regions: Record<string, { page_number: number }>;
    };
    expect(gt.lines?.[0]?.sku).toBe("PO-4711");
    expect(gt.regions["lines.0.sku"].page_number).toBe(1);
  });
});
