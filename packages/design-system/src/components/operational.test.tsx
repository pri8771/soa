import { render, screen } from "@testing-library/react";

import {
  ConfidenceIndicator,
  confidenceLevel,
  ConnectionStatusCard,
  formatSlaRemaining,
  SlaIndicator,
  StageProgress,
  StatusIndicator,
  UsageMeter,
  ValidationSummary,
} from "./operational";

const NOW = new Date("2026-07-12T12:00:00Z");

describe("StatusIndicator", () => {
  it.each([
    ["approved", "Approved"],
    ["review_required", "Needs review"],
    ["quarantined", "Quarantined"],
    ["failed", "Failed"],
  ] as const)("labels %s in text, not color alone", (state, label) => {
    render(<StatusIndicator state={state} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });
});

describe("ConfidenceIndicator", () => {
  it.each([
    [0.97, "high"],
    [0.9, "high"],
    [0.75, "medium"],
    [0.42, "low"],
  ])("maps %f to %s", (value, level) => {
    expect(confidenceLevel(value)).toBe(level);
  });

  it("shows label AND numeric detail", () => {
    render(<ConfidenceIndicator value={0.42} explanation="OCR quality below threshold" />);
    expect(screen.getByText("Low · 42%")).toBeInTheDocument();
    expect(screen.getByTitle("OCR quality below threshold")).toBeInTheDocument();
  });
});

describe("SlaIndicator", () => {
  it("formats remaining time deterministically", () => {
    expect(formatSlaRemaining(new Date("2026-07-12T15:30:00Z"), NOW)).toBe("3h 30m left");
    expect(formatSlaRemaining(new Date("2026-07-12T12:25:00Z"), NOW)).toBe("25m left");
    expect(formatSlaRemaining(new Date("2026-07-12T09:45:00Z"), NOW)).toBe("breached 2h 15m ago");
  });

  it("renders breached and at-risk states as text", () => {
    const { rerender } = render(
      <SlaIndicator dueAt={new Date("2026-07-12T09:45:00Z")} now={NOW} />,
    );
    expect(screen.getByText(/breached 2h 15m ago/)).toBeInTheDocument();
    rerender(<SlaIndicator dueAt={new Date("2026-07-12T12:30:00Z")} now={NOW} />);
    expect(screen.getByText(/30m left/)).toBeInTheDocument();
  });
});

describe("ValidationSummary", () => {
  it("pluralizes and splits errors and warnings", () => {
    render(<ValidationSummary errors={2} warnings={1} />);
    expect(screen.getByText("2 errors")).toBeInTheDocument();
    expect(screen.getByText("1 warning")).toBeInTheDocument();
  });

  it("shows Valid when clean", () => {
    render(<ValidationSummary errors={0} warnings={0} />);
    expect(screen.getByText("Valid")).toBeInTheDocument();
  });
});

describe("StageProgress", () => {
  it("marks the active stage with aria-current and names failures in text", () => {
    render(
      <StageProgress
        label="Processing stages"
        stages={[
          { id: "extract", label: "Extracting", status: "done" },
          { id: "normalize", label: "Normalizing", status: "active" },
          { id: "validate", label: "Validating", status: "pending" },
          { id: "deliver", label: "Delivering", status: "failed" },
        ]}
      />,
    );
    const active = screen.getByText("Normalizing").closest("li");
    expect(active).toHaveAttribute("aria-current", "step");
    expect(screen.getByText("Delivering (failed)")).toBeInTheDocument();
  });
});

describe("ConnectionStatusCard", () => {
  it("shows health in text with last-checked context", () => {
    render(
      <ConnectionStatusCard name="ERP webhook" health="degraded" lastCheckedLabel="2 minutes ago">
        <p>Retries are backing off.</p>
      </ConnectionStatusCard>,
    );
    expect(screen.getByText("ERP webhook")).toBeInTheDocument();
    expect(screen.getByText("Degraded")).toBeInTheDocument();
    expect(screen.getByText("Last checked 2 minutes ago")).toBeInTheDocument();
  });
});

describe("UsageMeter", () => {
  it("announces the fraction and warns near the limit", () => {
    render(<UsageMeter label="Documents this month" used={950} limit={1000} unit="docs" />);
    expect(screen.getByText(/950 \/ 1,000 docs — near limit/)).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "Documents this month usage" }),
    ).toHaveAttribute("aria-valuenow", "95");
  });

  it("stays quiet away from the limit", () => {
    render(<UsageMeter label="Pages" used={10} limit={1000} />);
    expect(screen.getByText(/10 \/ 1,000/)).toBeInTheDocument();
    expect(screen.queryByText(/near limit/)).not.toBeInTheDocument();
  });
});
