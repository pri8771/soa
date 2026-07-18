import { HttpResponse, http } from "msw";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_CLASSIFIER_DRAFT, server } from "../test/msw";
import { renderApp } from "../test/render";

const PATH = "/app/northstar/streams/email/classifier";

describe("Classifier builder (routing)", () => {
  it("renders the published draft's route, target skill, and signals", async () => {
    await renderApp(PATH);
    expect(await screen.findByDisplayValue("acme")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Acme GmbH")).toBeInTheDocument();
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("editing a signal marks unsaved changes and saves without If-Match", async () => {
    const user = userEvent.setup();
    let patched: { headers: Headers; body: unknown } | null = null;
    server.use(
      http.patch("/api/orgs/northstar/classifier-versions/:id", async ({ request }) => {
        patched = { headers: request.headers, body: await request.json() };
        return HttpResponse.json(DEFAULT_CLASSIFIER_DRAFT);
      }),
    );
    await renderApp(PATH);
    const signal = await screen.findByDisplayValue("Acme GmbH");
    await user.clear(signal);
    await user.type(signal, "Acme GmbH & Co");
    expect(await screen.findByText("Unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(patched).not.toBeNull());
    expect(patched!.headers.get("If-Match")).toBeNull();
    const body = patched!.body as { content: { routes: { signals: string[] }[] } };
    expect(body.content.routes[0].signals[0]).toBe("Acme GmbH & Co");
  });

  it("server rejection on save shows a banner without losing the edit", async () => {
    const user = userEvent.setup();
    server.use(
      http.patch("/api/orgs/northstar/classifier-versions/:id", () =>
        HttpResponse.json(
          {
            error: { message: "Route 'acme' targets a stream that does not exist or is archived." },
          },
          { status: 422 },
        ),
      ),
    );
    await renderApp(PATH);
    const label = await screen.findByDisplayValue("acme");
    await user.clear(label);
    await user.type(label, "other-label");
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText("The routing table was not saved")).toBeInTheDocument();
    expect(screen.getByText(/targets a stream that does not exist/)).toBeInTheDocument();
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(screen.getByDisplayValue("other-label")).toBeInTheDocument();
  });

  it("publish is disabled while there are unsaved changes", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    const publishButton = await screen.findByRole("button", { name: "Publish" });
    expect(publishButton).toBeEnabled();
    const label = screen.getByDisplayValue("acme");
    await user.clear(label);
    await user.type(label, "acme-2");
    expect(publishButton).toBeDisabled();
  });

  it("publish posts to the classifier version's publish endpoint", async () => {
    const user = userEvent.setup();
    let published = false;
    server.use(
      http.post("/api/orgs/northstar/classifier-versions/:id/publish", () => {
        published = true;
        return HttpResponse.json({ ...DEFAULT_CLASSIFIER_DRAFT, state: "published" });
      }),
    );
    await renderApp(PATH);
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    await waitFor(() => expect(published).toBe(true));
  });

  it("adding a route defaults its target to the first available skill", async () => {
    const user = userEvent.setup();
    await renderApp(PATH);
    await screen.findByDisplayValue("acme");
    await user.click(screen.getByRole("button", { name: "Add route" }));
    expect(screen.getAllByLabelText("Label")).toHaveLength(2);
    expect(screen.getAllByLabelText(/Signal 1/)).toHaveLength(2);
  });
});
