import { afterEach, describe, expect, it } from "vitest";

import { buildWorkerApp } from "../src/app.js";

const apps: ReturnType<typeof buildWorkerApp>[] = [];

afterEach(async () => {
  await Promise.all(apps.splice(0).map(async (app) => app.close()));
});

describe("worker health endpoints", () => {
  it("reports readiness independently from the API", async () => {
    const app = buildWorkerApp();
    apps.push(app);

    const response = await app.inject({ method: "GET", url: "/health/ready" });

    expect(response.statusCode).toBe(200);
    expect(response.json()).toMatchObject({
      service: "worker",
      status: "ok",
    });
  });
});
