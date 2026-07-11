import Fastify, { type FastifyInstance } from "fastify";

import { buildHealthResponse } from "@marketing-ops/contracts";

export function buildApiApp(): FastifyInstance {
  const app = Fastify({
    logger: process.env.NODE_ENV !== "test",
  });

  app.get("/", async () => ({
    service: "Marketing Ops API",
    status: "available",
  }));

  app.get("/health/live", async () =>
    buildHealthResponse({
      service: "api",
    }),
  );

  app.get("/health/ready", async () =>
    buildHealthResponse({
      service: "api",
    }),
  );

  return app;
}
