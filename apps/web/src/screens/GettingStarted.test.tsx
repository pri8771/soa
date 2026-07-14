import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

describe("Getting started checklist (GTM-003)", () => {
  it("shows the onboarding steps and a live progress bar", async () => {
    await renderApp("/app/northstar/getting-started");

    expect(await screen.findByRole("heading", { name: "Getting started" })).toBeInTheDocument();
    // The onboarding path, in order.
    expect(screen.getByText("Create your organization")).toBeInTheDocument();
    expect(screen.getByText("Publish a process and schema")).toBeInTheDocument();
    expect(screen.getByText("Set up a stream")).toBeInTheDocument();
    expect(screen.getByText("Import a catalog")).toBeInTheDocument();
    expect(screen.getByText("Process a sample document")).toBeInTheDocument();
    expect(screen.getByText("Connect an integration")).toBeInTheDocument();
    expect(screen.getByText("Go live")).toBeInTheDocument();

    // The progress bar reflects live configuration, not a stored flag.
    expect(screen.getByRole("progressbar", { name: /Setup progress/ })).toBeInTheDocument();
  });

  it("marks every data step to-do and links it when the tenant is empty", async () => {
    // A brand-new tenant: nothing configured yet. Each data-driven step is
    // to-do and exposes a link to the screen that completes it.
    server.use(
      http.get("/api/orgs/:slug/processes", () => HttpResponse.json([])),
      http.get("/api/orgs/:slug/streams", () => HttpResponse.json([])),
      http.get("/api/orgs/:slug/catalogs", () => HttpResponse.json({ items: [] })),
      http.get("/api/orgs/:slug/integrations", () => HttpResponse.json({ items: [] })),
      http.get("/api/orgs/:slug/documents", () =>
        HttpResponse.json({ items: [], has_more: false, next_cursor: null }),
      ),
    );

    await renderApp("/app/northstar/getting-started");

    // Each empty data step resolves to a link to the screen that completes it.
    expect(await screen.findByRole("link", { name: "Open Processes" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Open Streams" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Open Catalogs" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Upload a document" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Open Integrations" })).toBeInTheDocument();
    // The organization step is always complete once you're inside one.
    expect(screen.getAllByText("done").length).toBeGreaterThanOrEqual(1);
    // Not everything is done, so go-live is not yet ready (5 data steps +
    // the go-live step are all "to do" once every query has resolved).
    await waitFor(() => {
      expect(screen.getAllByText("to do").length).toBeGreaterThanOrEqual(5);
    });
  });
});
