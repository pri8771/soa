import { describe, expect, it } from "vitest";

import { createServiceConfig, parsePort } from "./index.js";

describe("configuration primitives", () => {
  it("uses a fallback port when a value is absent", () => {
    expect(parsePort(undefined, 4100)).toBe(4100);
  });

  it("rejects invalid ports", () => {
    expect(() => parsePort("70000", 4100)).toThrow("Invalid port");
  });

  it("creates a typed local service configuration", () => {
    expect(
      createServiceConfig({
        environment: "local",
        fallbackPort: 4100,
        serviceName: "api",
      }),
    ).toEqual({
      environment: "local",
      host: "0.0.0.0",
      port: 4100,
      serviceName: "api",
    });
  });
});
