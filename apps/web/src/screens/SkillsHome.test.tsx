import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

const SKILLS = {
  items: [
    {
      id: "s-1",
      slug: "uk",
      name: "Pharma Wholesale",
      status: "active",
      process_slug: "purchase-orders",
      process_name: "Purchase orders",
      in_review: 14,
      received_30d: 412,
      field_accuracy: 0.968,
      trained_version: 3,
    },
    {
      id: "s-2",
      slug: "spain",
      name: "Iberia Distribution",
      status: "active",
      process_slug: "purchase-orders",
      process_name: "Purchase orders",
      in_review: 0,
      received_30d: 218,
      field_accuracy: null,
      trained_version: null,
    },
  ],
};

describe("Skills home (Blueprint IA)", () => {
  it("groups skills by process with operating numbers and training state", async () => {
    server.use(http.get("/api/orgs/:slug/skills", () => HttpResponse.json(SKILLS)));
    await renderApp("/app/northstar/skills");

    expect(await screen.findByRole("heading", { name: "Purchase orders" })).toBeInTheDocument();
    expect(screen.getByText("Pharma Wholesale")).toBeInTheDocument();
    expect(screen.getByText("Trained · v3")).toBeInTheDocument();
    expect(screen.getByText("Needs training")).toBeInTheDocument();
    expect(screen.getByText("96.8%")).toBeInTheDocument();
    // Total queue rolls up into the review CTA.
    expect(screen.getByRole("button", { name: /Review queue · 14/ })).toBeInTheDocument();
    // Cards link to the skill dashboard (stream detail).
    const link = screen.getByRole("link", { name: /Pharma Wholesale/ });
    expect(link).toHaveAttribute("href", "/app/northstar/streams/uk");
  });
});

describe("Unrouted band", () => {
  it("lists unrouted documents and routes one to a chosen skill", async () => {
    let routed: unknown = null;
    server.use(
      http.get("/api/orgs/:slug/skills", () => HttpResponse.json(SKILLS)),
      http.get("/api/orgs/:slug/routing/unrouted", () =>
        HttpResponse.json({
          items: [
            {
              id: "doc-9",
              stream_id: "s-1",
              original_filename: "mystery.pdf",
              received_at: "2026-07-17T10:00:00+00:00",
              state_reason: "stage classifying failed: unrouted: no route matched",
            },
          ],
        }),
      ),
      http.post("/api/orgs/:slug/documents/:documentId/route", async ({ request, params }) => {
        routed = { documentId: params.documentId, ...((await request.json()) as object) };
        return HttpResponse.json({ document_id: "doc-9", stream_slug: "uk", state: "queued" });
      }),
    );
    const user = userEvent.setup();
    await renderApp("/app/northstar/skills");

    expect(await screen.findByText("mystery.pdf")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Unrouted · 1");

    // Pick a target skill and route it.
    await user.click(screen.getByRole("button", { name: /Route to skill/ }));
    await user.click(await screen.findByRole("option", { name: "Pharma Wholesale" }));
    await user.click(screen.getByRole("button", { name: "Route" }));
    await waitFor(() => expect(routed).toEqual({ documentId: "doc-9", stream_slug: "uk" }));
  });
});
