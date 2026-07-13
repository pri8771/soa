import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, vi } from "vitest";

import { SplitLayout } from "./SplitLayout";

function stubMatchMedia(matches: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockReturnValue({
      matches,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  );
}

function renderSplit() {
  return render(
    <SplitLayout
      storageKey="soa.test.split"
      leftLabel="Document"
      rightLabel="Fields"
      left={<div>the document viewer</div>}
      right={<div>the field editor</div>}
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("Review Studio split layout (REV-015)", () => {
  it("side-by-side: the separator resizes with the keyboard and persists", async () => {
    stubMatchMedia(false);
    const user = userEvent.setup();
    renderSplit();
    expect(screen.getByTestId("split-side-by-side")).toBeInTheDocument();
    const separator = screen.getByRole("separator", { name: /Resize/ });
    expect(separator).toHaveAttribute("aria-valuenow", "60"); // the default
    separator.focus();
    await user.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(separator).toHaveAttribute("aria-valuenow", "50");
    await user.keyboard("{ArrowRight}");
    expect(separator).toHaveAttribute("aria-valuenow", "55");
    // The preference persists for the next visit.
    expect(window.localStorage.getItem("soa.test.split")).toBe("55");
  });

  it("restores the persisted split on mount and clamps to sane bounds", () => {
    stubMatchMedia(false);
    window.localStorage.setItem("soa.test.split", "95"); // out of range
    renderSplit();
    expect(screen.getByRole("separator", { name: /Resize/ })).toHaveAttribute(
      "aria-valuenow",
      "75", // clamped to the maximum
    );
  });

  it("compact mode stacks the panels with nothing hidden", () => {
    stubMatchMedia(true);
    renderSplit();
    expect(screen.getByTestId("split-stacked")).toBeInTheDocument();
    // The defined read/approve order: document first, fields second —
    // and BOTH remain in the accessibility tree.
    const stacked = screen.getByTestId("split-stacked");
    const labels = [...stacked.querySelectorAll("[aria-label]")].map((node) =>
      node.getAttribute("aria-label"),
    );
    expect(labels).toEqual(["Document", "Fields"]);
    expect(screen.getByText("the document viewer")).toBeVisible();
    expect(screen.getByText("the field editor")).toBeVisible();
    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
  });
});
