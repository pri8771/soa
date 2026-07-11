import { createServiceConfig } from "@marketing-ops/config";

import { buildApiApp } from "./app.js";

const config = createServiceConfig({
  environment: process.env.MARKETING_OPS_ENV,
  fallbackPort: 4100,
  host: process.env.API_HOST,
  port: process.env.API_PORT,
  serviceName: "api",
});

const app = buildApiApp();

async function shutdown(signal: string) {
  app.log.info({ signal }, "shutting down API");
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
