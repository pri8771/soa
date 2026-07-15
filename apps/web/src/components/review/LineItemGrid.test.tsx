import { HttpResponse, http } from "msw";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DEFAULT_WORKSPACE, server } from "../../test/msw";
import { renderApp } from "../../test/render";

const PATH = `/app/northstar/review/${DEFAULT_WORKSPACE.task.id}`;

function captureCorrections(): unknown[] {
  const posted: unknown[] = [];
  server.use(
    http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      posted.push(body);
      return HttpResponse.json({
        correction: {
          id: `cor-${posted.length}`,
          field_key: body["field_key"],
          row_index: body["row_index"] ?? null,
          previous_raw_value: null,
          corrected_raw_value: body["value"],
          corrected_normalized_value: body["value"],
          normalization_error: null,
          corrected_by: "user:u-1",
        },
        task_version: 3 + posted.length,
        revalidation: null,
      });
    }),
  );
  return posted;
}

async function grid() {
  return await screen.findByRole("grid", { name: "lines rows" });
}

describe("Line-item grid (REV-008)", () => {
  it("renders rows with a frozen first column and a totals footer", async () => {
    await renderApp(PATH);
    const table = await grid();
    expect(within(table).getByDisplayValue("WID-100")).toBeInTheDocument();
    expect(within(table).getByDisplayValue("GAD-205")).toBeInTheDocument();
    // Frozen first column: the sku header is sticky.
    const skuHeader = within(table).getByRole("columnheader", { name: "sku" });
    expect(skuHeader.style.position).toBe("sticky");
    expect(skuHeader.style.left).toBe("0px");
    // Totals: quantity 10+3, line_total 450.00+784.50.
    expect(within(table).getByText("13.00")).toBeInTheDocument();
    expect(within(table).getByText("1234.50")).toBeInTheDocument();
  });

  it("saves a cell on Enter with its stable row identity and moves down the column", async () => {
    const user = userEvent.setup();
    const posted = captureCorrections();
    await renderApp(PATH);
    const table = await grid();
    const quantity = within(table).getByLabelText("quantity row 0");
    await user.clear(quantity);
    await user.type(quantity, "12{Enter}");
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({
      field_key: "lines.quantity",
      row_index: 0,
      value: "12",
      expected_version: 3,
    });
    // Spreadsheet flow: focus moved down the same column.
    expect(within(table).getByLabelText("quantity row 1")).toHaveFocus();
  });

  it("retries an unchanged cell value after a failed save and then unblocks approval", async () => {
    const user = userEvent.setup();
    let attempts = 0;
    server.use(
      http.post("/api/orgs/northstar/review-tasks/:taskId/corrections", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        attempts += 1;
        if (attempts === 1) {
          return HttpResponse.json(
            { error: { message: "temporary correction outage" } },
            { status: 503 },
          );
        }
        return HttpResponse.json({
          correction: {
            id: "cor-retry",
            field_key: body["field_key"],
            row_index: body["row_index"],
            previous_raw_value: "10",
            corrected_raw_value: body["value"],
            corrected_normalized_value: body["value"],
            normalization_error: null,
            corrected_by: "user:u-1",
          },
          task_version: 4,
          revalidation: null,
        });
      }),
    );
    await renderApp(PATH);
    const table = await grid();
    const quantity = within(table).getByLabelText("quantity row 0");
    await user.clear(quantity);
    await user.type(quantity, "12{Enter}");

    expect(
      await within(table).findByText(/Not saved: temporary correction outage/),
    ).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "Approve order…" });
    expect(approve).toBeDisabled();

    // The draft is intentionally unchanged. Pressing Enter again must retry
    // that exact value rather than being suppressed as a duplicate.
    await user.click(quantity);
    await user.keyboard("{Enter}");
    await waitFor(() => expect(attempts).toBe(2));
    await waitFor(() => expect(approve).toBeEnabled());
  });

  it("remove is a batch of clearing corrections, and undo restores the values", async () => {
    const user = userEvent.setup();
    const posted = captureCorrections();
    await renderApp(PATH);
    const table = await grid();
    await user.click(within(table).getByRole("button", { name: "Remove row 1" }));
    await waitFor(() => expect(posted).toHaveLength(3)); // sku, quantity, line_total
    expect(posted).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field_key: "lines.sku", row_index: 1, value: null }),
        expect.objectContaining({ field_key: "lines.quantity", row_index: 1, value: null }),
        expect.objectContaining({ field_key: "lines.line_total", row_index: 1, value: null }),
      ]),
    );
    // Destructive edit is undoable: the previous values come back as
    // NEW corrections — append-only history, nothing destroyed.
    await user.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(posted).toHaveLength(6));
    expect(posted.slice(3)).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field_key: "lines.sku", row_index: 1, value: "GAD-205" }),
        expect.objectContaining({ field_key: "lines.quantity", row_index: 1, value: "3" }),
        expect.objectContaining({
          field_key: "lines.line_total",
          row_index: 1,
          value: "784.50",
        }),
      ]),
    );
  });

  it("add row creates a fresh row identity; typing persists onto it", async () => {
    const user = userEvent.setup();
    const posted = captureCorrections();
    await renderApp(PATH);
    await grid();
    await user.click(screen.getByRole("button", { name: "Add row" }));
    const sku = await screen.findByLabelText("sku row 2");
    await user.type(sku, "NEW-1{Enter}");
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toMatchObject({ field_key: "lines.sku", row_index: 2, value: "NEW-1" });
  });

  it("split copies the row onto a new identity; merge folds quantities up and clears", async () => {
    const user = userEvent.setup();
    const posted = captureCorrections();
    await renderApp(PATH);
    const table = await grid();
    await user.click(within(table).getByRole("button", { name: "Split row 0" }));
    await waitFor(() => expect(posted.length).toBeGreaterThanOrEqual(4));
    expect(posted).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field_key: "lines.sku", row_index: 2, value: "WID-100" }),
        expect.objectContaining({ field_key: "lines.quantity", row_index: 2, value: "10" }),
      ]),
    );

    posted.length = 0;
    await user.click(within(table).getByRole("button", { name: "Merge row 1 into the row above" }));
    await waitFor(() => expect(posted.length).toBeGreaterThanOrEqual(5));
    expect(posted).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field_key: "lines.quantity", row_index: 0, value: "13" }),
        expect.objectContaining({ field_key: "lines.line_total", row_index: 0, value: "1234.5" }),
        expect.objectContaining({ field_key: "lines.sku", row_index: 1, value: null }),
      ]),
    );
  });

  it("paste fills tab-separated cells rightward from the focused column", async () => {
    const user = userEvent.setup();
    const posted = captureCorrections();
    await renderApp(PATH);
    const table = await grid();
    const sku = within(table).getByLabelText("sku row 0");
    await user.click(sku);
    await user.paste("ZZZ-9\tHeavy widget\t4\t25.00\t100.00");
    await waitFor(() => expect(posted).toHaveLength(5));
    expect(posted).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ field_key: "lines.sku", row_index: 0, value: "ZZZ-9" }),
        expect.objectContaining({
          field_key: "lines.description",
          row_index: 0,
          value: "Heavy widget",
        }),
        expect.objectContaining({ field_key: "lines.line_total", row_index: 0, value: "100.00" }),
      ]),
    );
  });

  it("stays responsive on hundreds of rows: only the window renders", async () => {
    const bigRows = Array.from({ length: 300 }, (_, index) => [
      {
        field_key: "lines.sku",
        row_index: index,
        raw_value: `SKU-${index}`,
        normalized_value: `SKU-${index}`,
        normalization_error: null,
        confidence: 0.9,
        validation_status: "passed",
        provider: "mock",
        provider_model: null,
        evidence: [],
        candidates: [],
      },
      {
        field_key: "lines.quantity",
        row_index: index,
        raw_value: "1",
        normalized_value: "1",
        normalization_error: null,
        confidence: 0.9,
        validation_status: "passed",
        provider: "mock",
        provider_model: null,
        evidence: [],
        candidates: [],
      },
    ]);
    server.use(
      http.get("/api/orgs/northstar/review-tasks/:taskId/workspace", () =>
        HttpResponse.json({ ...DEFAULT_WORKSPACE, line_items: { lines: bigRows } }),
      ),
    );
    await renderApp(PATH);
    const table = await grid();
    expect(table).toHaveAttribute("aria-rowcount", "300");
    expect(screen.getByText("300 row(s)")).toBeInTheDocument();
    // Windowed: far fewer than 300 rows are mounted.
    const mounted = within(table).getAllByLabelText(/sku row \d+/);
    expect(mounted.length).toBeLessThan(60);
    expect(within(table).getByLabelText("sku row 0")).toBeInTheDocument();
    expect(within(table).queryByLabelText("sku row 250")).not.toBeInTheDocument();
  });
});
