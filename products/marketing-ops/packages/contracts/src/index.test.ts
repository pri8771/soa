import { describe, expect, it } from "vitest";

import { buildHealthResponse } from "./index.js";

describe("health response contract", () => {
  it("builds a deterministic response when a clock is supplied", () => {
    const response = buildHealthResponse({
      service: "api",
      now: new Date("2026-07-11T12:00:00.000Z"),
    });

    expect(response).toEqual({
      checkedAt: "2026-07-11T12:00:00.000Z",
      service: "api",
      status: "ok",
      version: "0.1.0",
    });
  });
});
