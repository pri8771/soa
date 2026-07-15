import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import type { CanonicalPayloadResponse } from "../../api/client";
import type { CanonicalOrder, LineItem } from "../../api/canonical-order";
import { PayloadViewer } from "./PayloadViewer";

function order(lineCount = 2): CanonicalOrder {
  const line_items: LineItem[] = Array.from({ length: lineCount }, (_, index) => ({
    line_number: index + 1,
    sku: `SKU-${index + 1}`,
    description: `Item ${index + 1}`,
    quantity: "10",
    unit_price: { amount: "45.00", currency: "USD" },
    line_total: { amount: "450.00", currency: "USD" },
  }));
  return {
    schema_version: "1.0.0",
    identifiers: { po_number: "PO-100042" },
    parties: { buyer: { name: "Acme Industrial" } },
    dates: { order_date: "2026-03-14" },
    terms: { currency: "USD" },
    totals: { grand_total: { amount: "1234.50", currency: "USD" } },
    line_items,
    source: {
      document_id: "8a111111-1111-4111-8111-111111111111",
      run_id: "a2222222-2222-4222-8222-222222222222",
    },
    provenance: {
      "identifiers.po_number": { origin: "corrected", actor: "user:u-1" },
    },
  };
}

function response(overrides: Partial<CanonicalPayloadResponse> = {}): CanonicalPayloadResponse {
  return {
    document_id: "8a111111-1111-4111-8111-111111111111",
    run_id: "a2222222-2222-4222-8222-222222222222",
    schema_version: "1.0.0",
    sha256: "ab".repeat(32),
    created_at: "2026-07-13T06:00:00+00:00",
    created_by: "user:supervisor",
    can_copy: true,
    redacted: false,
    payload: order(),
    ...overrides,
  };
}

describe("Canonical payload viewer (CAN-004)", () => {
  it("shows the formatted business view by default and switches views", async () => {
    const user = userEvent.setup();
    render(<PayloadViewer data={response()} />);
    expect(screen.getByText("PO-100042")).toBeInTheDocument();
    expect(screen.getByText("Acme Industrial")).toBeInTheDocument();
    expect(screen.getByText("1234.50 USD")).toBeInTheDocument();
    expect(screen.getByText(/Line items \(2\)/)).toBeInTheDocument();
    expect(screen.getByText("SKU-2")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Tree" }));
    const tree = screen.getByTestId("payload-tree");
    expect(within(tree).getByText(/line_items \(2\)/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "JSON" }));
    expect(screen.getByTestId("payload-json")).toHaveTextContent('"po_number": "PO-100042"');
  });

  it("copies the JSON with an announced outcome", async () => {
    const user = userEvent.setup();
    render(<PayloadViewer data={response()} />);
    await user.click(screen.getByRole("button", { name: "Copy JSON" }));
    expect(await screen.findByText("Payload copied to the clipboard.")).toBeInTheDocument();
    const copied = await navigator.clipboard.readText();
    expect(copied).toContain('"po_number": "PO-100042"');
    expect(screen.getByRole("status")).toHaveTextContent("copied");
  });

  it("downloads through an object URL", async () => {
    const user = userEvent.setup();
    const createObjectURL = vi.fn(() => "blob:mock");
    const revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    render(<PayloadViewer data={response()} />);
    await user.click(screen.getByRole("button", { name: "Download" }));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock");
    expect(screen.getByText("Payload downloaded.")).toBeInTheDocument();
    click.mockRestore();
  });

  it("copy/download are permission-gated with the reason visible, and redaction announces itself", () => {
    render(
      <PayloadViewer data={response({ can_copy: false, redacted: true, created_by: null })} />,
    );
    expect(screen.getByRole("button", { name: "Copy JSON" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Download" })).toBeDisabled();
    expect(screen.getByText(/needs the documents.review permission/)).toBeInTheDocument();
    expect(screen.getByText("redacted view")).toBeInTheDocument();
    expect(screen.getByText(/removed by the server/)).toBeInTheDocument();
  });

  it("stays safe on large payloads: capped table, capped tree, truncated JSON preview", async () => {
    const user = userEvent.setup();
    render(<PayloadViewer data={response({ payload: order(250) })} />);
    // Formatted table caps at 100 rows and says so.
    expect(screen.getByText(/first 100 of 250 lines/)).toBeInTheDocument();
    expect(screen.queryByText("SKU-101")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Tree" }));
    expect(screen.getByText(/150 more — download the JSON/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "JSON" }));
    expect(screen.getByText("Preview truncated")).toBeInTheDocument();
    const rendered = screen.getByTestId("payload-json").textContent ?? "";
    expect(rendered.length).toBeLessThanOrEqual(20_000);
  });
});
