import { describe, expect, it } from "vitest";

import { summarizePortfolioHealth } from "./portfolio-health";

describe("portfolio health", () => {
  it("summarizes actionable campaign health", () => {
    expect(
      summarizePortfolioHealth([
        { blocked: 2, dueSoon: 3, status: "at-risk" },
        { blocked: 0, dueSoon: 1, status: "on-track" },
        { blocked: 0, dueSoon: 0, status: "on-track" },
      ]),
    ).toEqual({
      blockedItems: 2,
      dueSoonItems: 4,
      onTrackRate: 67,
    });
  });
});
