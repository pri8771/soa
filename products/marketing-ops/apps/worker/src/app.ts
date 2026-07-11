import Fastify, { type FastifyInstance } from "fastify";

import { buildHealthResponse } from "@marketing-ops/contracts";

export function buildWorkerApp(): FastifyInstance {
  const app = Fastify({
    logger: process.env.NODE_ENV !== "test",
  });

  app.get("/health/live", async () =>
    buildHealthResponse({
      service: "worker",
    }),
  );

  app.get("/health/ready", async () =>
    buildHealthResponse({
      service: "worker",
    }),
  );

  app.get("/jobs/status", async () => ({
    active: 0,
    mode: "foundation-placeholder",
    queued: 0,
  }));

  return app;
}
