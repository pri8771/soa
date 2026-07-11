import { describe, expect, it } from "vitest";

import { DeterministicMockAiProvider } from "./index.js";

describe("deterministic AI provider", () => {
  it("returns reproducible candidates without an external call", async () => {
    const provider = new DeterministicMockAiProvider();
    const result = await provider.generate({
      instruction: "Adapt for LinkedIn",
      sourceText: "Launch   the campaign",
    });

    expect(result.content).toBe("Adapt for LinkedIn: Launch the campaign");
    expect(result.provider).toBe("mock-deterministic");
  });
});
