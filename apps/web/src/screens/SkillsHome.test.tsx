import { HttpResponse, http } from "msw";
import { screen } from "@testing-library/react";

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
