import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { ApprovalPanel, type ApprovalPanelProps } from "./ApprovalPanel";

function renderPanel(overrides: Partial<ApprovalPanelProps> = {}) {
  const onApprove = vi.fn();
  const onReject = vi.fn();
  const onEscalate = vi.fn();
  render(
    <ApprovalPanel
      editable
      readOnlyReason={null}
      blocking={false}
      decision={{ route: "approved", reasons: [] }}
      canApprove
      canOverride={false}
      canReject
      destination="Uploads"
      settledOutcome={null}
      busy={false}
      statusMessage={null}
      onApprove={onApprove}
      onReject={onReject}
      onEscalate={onEscalate}
      {...overrides}
    />,
  );
  return { onApprove, onReject, onEscalate };
}

describe("Approval panel (REV-013)", () => {
  it("approves only through the explicit two-step confirm", async () => {
    const user = userEvent.setup();
    const { onApprove } = renderPanel();
    // Step one opens the completion summary — nothing is approved yet.
    await user.click(screen.getByRole("button", { name: "Approve order…" }));
    expect(onApprove).not.toHaveBeenCalled();
    expect(screen.getByText(/All validations pass/)).toBeInTheDocument();
    expect(screen.getByText(/handed to export for stream “Uploads”/)).toBeInTheDocument();
    // Step two confirms.
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(onApprove).toHaveBeenCalledWith(null);
  });

  it("a stray Enter keypress does not approve", async () => {
    const user = userEvent.setup();
    const { onApprove } = renderPanel({ blocking: true, canOverride: true });
    await user.click(screen.getByRole("button", { name: "Approve order…" }));
    // Typing Enter in the override textarea inserts a newline; it never
    // submits the approval.
    const reason = screen.getByLabelText(/Override reason/);
    await user.type(reason, "checked{Enter}with the customer{Enter}");
    expect(onApprove).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));
    expect(onApprove).toHaveBeenCalledTimes(1);
  });

  it("lists remaining warnings in the completion summary", async () => {
    const user = userEvent.setup();
    renderPanel({
      decision: {
        route: "review_required",
        reasons: [
          {
            code: "rule_triggered",
            message: "Customer name is required",
            rule_key: "required.customer_name",
            field_key: null,
          },
        ],
      },
    });
    await user.click(screen.getByRole("button", { name: "Approve order…" }));
    expect(screen.getByText(/1 finding\(s\) remain/)).toBeInTheDocument();
    expect(screen.getByText("required.customer_name")).toBeInTheDocument();
    expect(screen.getByText(/Customer name is required/)).toBeInTheDocument();
  });

  it("shows WHY approval is disabled: not the assignee", () => {
    renderPanel({
      editable: false,
      readOnlyReason: "This task is assigned to user:u-2; claim it from the queue to edit.",
    });
    expect(screen.getByRole("button", { name: "Approve order…" })).toBeDisabled();
    expect(screen.getByText(/assigned to user:u-2/)).toBeInTheDocument();
  });

  it("shows WHY approval is disabled: missing the approve permission", () => {
    renderPanel({ canApprove: false });
    expect(screen.getByRole("button", { name: "Approve order…" })).toBeDisabled();
    expect(screen.getByText(/documents.approve permission/)).toBeInTheDocument();
  });

  it("blockers without the override permission disable confirm with the reason", async () => {
    const user = userEvent.setup();
    const { onApprove } = renderPanel({ blocking: true, canOverride: false });
    await user.click(screen.getByRole("button", { name: "Approve order…" }));
    expect(screen.getByRole("button", { name: "Confirm approval" })).toBeDisabled();
    expect(screen.getByText(/documents\.approve\.override/)).toBeInTheDocument();
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("blockers with the override permission require a written reason", async () => {
    const user = userEvent.setup();
    const { onApprove } = renderPanel({ blocking: true, canOverride: true });
    await user.click(screen.getByRole("button", { name: "Approve order…" }));
    const confirm = screen.getByRole("button", { name: "Confirm approval" });
    expect(confirm).toBeDisabled();
    expect(screen.getByText(/Enter the override reason/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/Override reason/), "verified with the buyer");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(onApprove).toHaveBeenCalledWith("verified with the buyer");
  });

  it("rejection requires a reason and the reject permission", async () => {
    const user = userEvent.setup();
    const { onReject } = renderPanel();
    await user.click(screen.getByRole("button", { name: "Reject…" }));
    const confirm = screen.getByRole("button", { name: "Confirm rejection" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText(/Rejection reason/), "This is a quote, not a PO.");
    await user.click(confirm);
    expect(onReject).toHaveBeenCalledWith("This is a quote, not a PO.");
  });

  it("without the reject permission the button is disabled and says why", () => {
    renderPanel({ canReject: false });
    expect(screen.getByRole("button", { name: "Reject…" })).toBeDisabled();
    expect(screen.getByText(/documents.reject permission/)).toBeInTheDocument();
  });

  it("escalation collects a reason", async () => {
    const user = userEvent.setup();
    const { onEscalate } = renderPanel();
    await user.click(screen.getByRole("button", { name: "Escalate…" }));
    const confirm = screen.getByRole("button", { name: "Confirm escalation" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText(/Escalation reason/), "needs a supervisor decision");
    await user.click(confirm);
    expect(onEscalate).toHaveBeenCalledWith("needs a supervisor decision");
  });

  it("announces outcomes through an aria-live status region", () => {
    renderPanel({ statusMessage: "Order approved." });
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Order approved.");
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("a settled task shows its outcome instead of actions", () => {
    renderPanel({ settledOutcome: "approved" });
    expect(screen.queryByRole("button", { name: "Approve order…" })).not.toBeInTheDocument();
    expect(screen.getByText("This task is approved")).toBeInTheDocument();
  });
});
