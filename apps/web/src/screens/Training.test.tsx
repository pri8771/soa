import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

function trainingSet(overrides: Record<string, unknown> = {}) {
  return {
    id: "ts-1",
    slug: "uk-samples",
    name: "UK samples",
    description: null,
    stream_id: "stream-1",
    privacy_classification: "customer_confidential",
    working_draft_version_id: "v-1",
    published_version_id: null,
    document_count: 3,
    ...overrides,
  };
}

describe("Training workspace (extraction-training Phase 1)", () => {
  it("lists a stream's training sets", async () => {
    server.use(
      http.get("/api/orgs/:slug/streams/:stream/training-sets", () =>
        HttpResponse.json({ items: [trainingSet()] }),
      ),
    );
    await renderApp("/app/northstar/streams/email/training");
    expect(await screen.findByRole("link", { name: "UK samples" })).toBeInTheDocument();
    expect(screen.getByText(/3 samples/)).toBeInTheDocument();
  });

  it("creates a training set and derives the slug from the name", async () => {
    let created: unknown = null;
    server.use(
      http.get("/api/orgs/:slug/streams/:stream/training-sets", () =>
        HttpResponse.json({ items: [] }),
      ),
      http.post("/api/orgs/:slug/streams/:stream/training-sets", async ({ request }) => {
        created = await request.json();
        return HttpResponse.json(trainingSet({ document_count: 0 }), { status: 201 });
      }),
    );
    const user = userEvent.setup();
    await renderApp("/app/northstar/streams/email/training");
    await screen.findByText("No training sets yet.");

    await user.type(screen.getByPlaceholderText("e.g. UK purchase orders"), "UK Purchase Orders");
    await user.click(screen.getByRole("button", { name: "Create training set" }));

    await waitFor(() =>
      expect(created).toEqual({ name: "UK Purchase Orders", slug: "uk-purchase-orders" }),
    );
  });
});
