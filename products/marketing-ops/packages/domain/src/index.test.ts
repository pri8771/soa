import { describe, expect, it } from "vitest";

import { calculateCampaignReadiness } from "./index.js";

describe("campaign readiness", () => {
  it("explains blocking requirements instead of returning a vague percentage", () => {
    const result = calculateCampaignReadiness([
      { id: "brief", label: "Brief approved", complete: true, blocking: true },
      { id: "legal", label: "Legal review", complete: false, blocking: true },
      { id: "utm", label: "UTM naming", complete: false, blocking: false },
    ]);

    expect(result.state).toBe("blocked");
    expect(result.completed).toBe(1);
    expect(result.unresolved.map((item) => item.id)).toEqual(["legal", "utm"]);
  });
});
