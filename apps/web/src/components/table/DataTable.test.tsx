import type { ColumnDef, SortingState } from "@tanstack/react-table";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { DataTable } from "./DataTable";

interface Row {
  id: string;
  reference: string;
  customer: string;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- matches DataTable prop type
const COLUMNS: ColumnDef<Row, any>[] = [
  { accessorKey: "reference", header: "Reference" },
  { accessorKey: "customer", header: "Customer", enableSorting: false },
];

function makeRows(count: number): Row[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `row-${index + 1}`,
    reference: `PO-${90000 + index}`,
    customer: `Customer ${index + 1}`,
  }));
}

describe("DataTable semantics and sorting", () => {
  it("renders a real table with caption and scoped headers", () => {
    render(
      <DataTable
        caption="Documents queue"
        columns={COLUMNS}
        data={makeRows(3)}
        getRowId={(row) => row.id}
      />,
    );
    const table = screen.getByRole("table", { name: "Documents queue" });
    const headers = within(table).getAllByRole("columnheader");
    expect(headers.map((h) => h.getAttribute("scope"))).toEqual(["col", "col"]);
  });

  it("cycles sort asc -> desc -> none and exposes aria-sort", async () => {
    const user = userEvent.setup();
    const changes: SortingState[] = [];

    function Harness() {
      const [sorting, setSorting] = useState<SortingState>([]);
      return (
        <DataTable
          caption="Sortable"
          columns={COLUMNS}
          data={makeRows(2)}
          getRowId={(row) => row.id}
          sorting={sorting}
          onSortingChange={(next) => {
            changes.push(next);
            setSorting(next);
          }}
        />
      );
    }

    render(<Harness />);
    const sortButton = screen.getByRole("button", { name: /Reference/ });
    await user.click(sortButton);
    expect(screen.getByRole("columnheader", { name: /Reference/ })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );
    await user.click(sortButton);
    expect(screen.getByRole("columnheader", { name: /Reference/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    await user.click(sortButton);
    expect(changes).toEqual([
      [{ id: "reference", desc: false }],
      [{ id: "reference", desc: true }],
      [],
    ]);
    // Unsortable column renders no sort button.
    expect(screen.queryByRole("button", { name: /Customer/ })).not.toBeInTheDocument();
  });
});

describe("DataTable selection and bulk bar", () => {
  function SelectableHarness() {
    const [selected, setSelected] = useState<Set<string>>(new Set());
    return (
      <DataTable
        caption="Selectable"
        columns={COLUMNS}
        data={makeRows(3)}
        getRowId={(row) => row.id}
        selectedIds={selected}
        onSelectionChange={setSelected}
        bulkActions={<button type="button">Reprocess</button>}
      />
    );
  }

  it("selects rows, shows the bulk bar with count, clears selection", async () => {
    const user = userEvent.setup();
    render(<SelectableHarness />);
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "Select row row-1" }));
    await user.click(screen.getByRole("checkbox", { name: "Select row row-3" }));
    const toolbar = screen.getByRole("toolbar", { name: "Bulk actions" });
    expect(within(toolbar).getByText("2 selected")).toBeInTheDocument();
    expect(within(toolbar).getByRole("button", { name: "Reprocess" })).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "Select all rows" }));
    expect(screen.getByText("3 selected")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });
});

describe("DataTable states", () => {
  it("renders skeleton rows while loading", () => {
    const { container } = render(
      <DataTable
        caption="Loading"
        columns={COLUMNS}
        data={[]}
        getRowId={(row: Row) => row.id}
        status="loading"
      />,
    );
    expect(container.querySelectorAll(".soa-skeleton").length).toBeGreaterThan(8);
  });

  it("renders a retryable error state", async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(
      <DataTable
        caption="Errored"
        columns={COLUMNS}
        data={[]}
        getRowId={(row: Row) => row.id}
        status="error"
        errorMessage="The queue service timed out."
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("The queue service timed out.");
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalled();
  });

  it("renders the designed empty state with a next action", () => {
    render(
      <DataTable
        caption="Empty"
        columns={COLUMNS}
        data={[]}
        getRowId={(row: Row) => row.id}
        emptyTitle="No documents yet"
        emptyBody="Upload a purchase order to see it here."
        emptyAction={<button type="button">Upload</button>}
      />,
    );
    expect(screen.getByText("No documents yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload" })).toBeInTheDocument();
  });

  it("offers load-more for cursor pagination", async () => {
    const user = userEvent.setup();
    const onLoadMore = vi.fn();
    render(
      <DataTable
        caption="Paged"
        columns={COLUMNS}
        data={makeRows(2)}
        getRowId={(row) => row.id}
        hasMore
        onLoadMore={onLoadMore}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(onLoadMore).toHaveBeenCalled();
  });
});

describe("DataTable virtualization (performance smoke)", () => {
  it("renders far fewer DOM rows than the 2000-row dataset", () => {
    render(
      <DataTable
        caption="Huge"
        columns={COLUMNS}
        data={makeRows(2000)}
        getRowId={(row) => row.id}
        virtualizeAt={100}
      />,
    );
    const bodyRows = screen.getAllByRole("row");
    // header + virtualized window (+ padding rows), not 2000.
    expect(bodyRows.length).toBeLessThan(120);
  });

  it("activates rows with Enter when onRowActivate is provided", async () => {
    const user = userEvent.setup();
    const onActivate = vi.fn();
    render(
      <DataTable
        caption="Activatable"
        columns={COLUMNS}
        data={makeRows(2)}
        getRowId={(row) => row.id}
        onRowActivate={onActivate}
      />,
    );
    const firstRow = screen.getByText("PO-90000").closest("tr");
    firstRow?.focus();
    await user.keyboard("{Enter}");
    expect(onActivate).toHaveBeenCalledWith(expect.objectContaining({ reference: "PO-90000" }));
  });
});
