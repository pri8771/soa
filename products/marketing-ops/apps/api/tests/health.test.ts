import { afterEach, describe, expect, it } from "vitest";

import { buildApiApp } from "../src/app.js";

const apps: ReturnType<typeof buildApiApp>[] = [];

afterEach(async () => {
  await Promise.all(apps.splice(0).map(async (app) => app.close()));
});

describe("API health endpoints", () => {
  it("returns the shared health contract", async () => {
    const app = buildApiApp();
    apps.push(app);

    const response = await app.inject({ method: "GET", url: "/health/ready" });

    expect(response.statusCode).toBe(200);
    expect(response.json()).toMatchObject({
      service: "api",
      status: "ok",
      version: "0.1.0",
    });
  });
});
