import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { server } from "../test/msw";
import { renderApp } from "../test/render";

const CSV_FILE = new File(["sku,name\nSKU-1,Widget\n,missing id\n"], "products.csv", {
  type: "text/csv",
});

describe("Catalogs list (CAT-005)", () => {
  it("lists catalogs with their live-version status", async () => {
    await renderApp("/app/northstar/catalogs");
    expect(await screen.findByText("Products")).toBeInTheDocument();
    expect(screen.getByText("Customers")).toBeInTheDocument();
    expect(screen.getByText("active version live")).toBeInTheDocument();
    expect(screen.getByText("no active version")).toBeInTheDocument();
  });
});

describe("Catalog manager (CAT-005)", () => {
  it("shows versions with states and browses records with search", async () => {
    const user = userEvent.setup();
    await renderApp("/app/northstar/catalogs/products");
    expect(await screen.findByText("v1")).toBeInTheDocument();
    const versions = screen.getByRole("region", { name: "Versions" });
    expect(within(versions).getByText("active")).toBeInTheDocument();
    expect(within(versions).getByText("draft")).toBeInTheDocument();

    await user.click(within(versions).getAllByRole("button", { name: "Browse records" })[0]);
    const records = await screen.findByRole("region", { name: "Records" });
    expect(await within(records).findByText("Widget 9mm")).toBeInTheDocument();
    expect(within(records).getByText("Flange Kit")).toBeInTheDocument();

    await user.type(within(records).getByLabelText("Search records"), "widget");
    await waitFor(() => expect(within(records).queryByText("Flange Kit")).not.toBeInTheDocument());
    expect(within(records).getByText("Widget 9mm")).toBeInTheDocument();
  });

  it("activates a draft version explicitly and confirms the switch", async () => {
    const user = userEvent.setup();
    const activated: string[] = [];
    server.use(
      http.post(
        "/api/orgs/northstar/catalogs/products/versions/:versionId/activate",
        ({ params }) => {
          activated.push(String(params.versionId));
          return HttpResponse.json({
            id: params.versionId,
            version_number: 2,
            state: "published",
            record_count: 3,
            change_summary: null,
            published_at: "2026-07-13T10:00:00Z",
            published_by: "user:u-1",
          });
        },
      ),
    );
    await renderApp("/app/northstar/catalogs/products");
    await screen.findByText("v2");
    await user.click(screen.getByRole("button", { name: "Activate" }));
    expect(await screen.findByText("v2 is now active")).toBeInTheDocument();
    expect(activated).toEqual(["catv-2"]);
  });

  it("runs the import wizard: dry-run report with CLEAR partial failures", async () => {
    const user = userEvent.setup();
    await renderApp("/app/northstar/catalogs/products");
    const wizard = await screen.findByRole("region", { name: "Import records" });

    await user.upload(within(wizard).getByLabelText(/File \(.csv/), CSV_FILE);
    await user.click(within(wizard).getByRole("button", { name: "Preview (dry run)" }));

    expect(
      await within(wizard).findByText(
        /2 valid rows · 1 failed rows · added 1, changed 1, deactivated 1/,
      ),
    ).toBeInTheDocument();
    // The failed row is named with its row number — partial failure is clear.
    const failures = within(wizard).getByRole("table", { name: /Failed rows/ });
    expect(within(failures).getByText("3")).toBeInTheDocument();
    expect(within(failures).getByText("missing 'sku'")).toBeInTheDocument();
  });

  it("refuses a partial import without the explicit checkbox, then creates the draft with it", async () => {
    const user = userEvent.setup();
    const bodies: Array<{ allow_partial?: boolean; dry_run?: boolean }> = [];
    server.use(
      http.post("/api/orgs/northstar/catalogs/products/imports", async ({ request }) => {
        const body = (await request.json()) as { allow_partial?: boolean; dry_run?: boolean };
        bodies.push(body);
        if (!body.allow_partial) {
          return HttpResponse.json(
            { error: { message: "1 rows failed validation; fix the file or pass allow_partial" } },
            { status: 422 },
          );
        }
        return HttpResponse.json({
          status: "draft_created",
          records: 2,
          issues: [{ row_number: 3, message: "missing 'sku'" }],
          warnings: [],
          encoding: "utf-8",
          preview: { added: ["SKU-3"], changed: [], deactivated: [], unchanged: 1 },
          version: {
            id: "catv-3",
            version_number: 3,
            state: "draft",
            record_count: 2,
            change_summary: null,
            published_at: null,
            published_by: null,
          },
        });
      }),
    );
    await renderApp("/app/northstar/catalogs/products");
    const wizard = await screen.findByRole("region", { name: "Import records" });
    await user.upload(within(wizard).getByLabelText(/File \(.csv/), CSV_FILE);

    await user.click(within(wizard).getByRole("button", { name: "Create draft version" }));
    expect(await within(wizard).findByText("Import refused")).toBeInTheDocument();
    expect(within(wizard).getByText(/allow_partial/)).toBeInTheDocument();

    await user.click(within(wizard).getByLabelText(/Import valid rows even if some fail/));
    await user.click(within(wizard).getByRole("button", { name: "Create draft version" }));
    expect(await within(wizard).findByText("Draft v3 created")).toBeInTheDocument();
    expect(
      within(wizard).getByText(/activate explicitly — imports never go live on their own/),
    ).toBeInTheDocument();
    expect(bodies.map((body) => body.allow_partial)).toEqual([false, true]);
  });
});
