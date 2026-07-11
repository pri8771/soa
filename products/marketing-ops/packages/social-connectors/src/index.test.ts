import { describe, expect, it } from "vitest";

import { mockSocialConnector } from "./index.js";

describe("mock social connector", () => {
  it("declares capabilities instead of assuming every provider behaves identically", () => {
    expect(mockSocialConnector.capabilities).toMatchObject({
      analytics: true,
      network: "mock",
      textPosts: true,
    });
  });
});
