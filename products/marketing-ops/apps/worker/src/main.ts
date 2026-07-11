import { createServiceConfig } from "@marketing-ops/config";

import { buildWorkerApp } from "./app.js";

const config = createServiceConfig({
  environment: process.env.MARKETING_OPS_ENV,
  fallbackPort: 4101,
  host: process.env.WORKER_HOST,
  port: process.env.WORKER_PORT,
  serviceName: "worker",
});

const app = buildWorkerApp();

async function shutdown(signal: string) {
  app.log.info({ signal }, "shutting down worker health service");
  await app.close();
  process.exit(0);
}

process.on("SIGINT", () => void shutdown("SIGINT"));
process.on("SIGTERM", () => void shutdown("SIGTERM"));

try {
  await app.listen({ host: config.host, port: config.port });
} catch (error) {
  app.log.error(error);
  process.exit(1);
}
