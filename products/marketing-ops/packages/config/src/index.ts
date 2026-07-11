export type RuntimeEnvironment = "local" | "test" | "development" | "staging" | "production";

export interface ServiceConfig {
  readonly environment: RuntimeEnvironment;
  readonly host: string;
  readonly port: number;
  readonly serviceName: string;
}

export function parsePort(value: string | undefined, fallback: number): number {
  if (value === undefined || value.trim() === "") {
    return fallback;
  }

  const port = Number.parseInt(value, 10);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`Invalid port: ${value}`);
  }

  return port;
}

export function parseEnvironment(value: string | undefined): RuntimeEnvironment {
  const environment = value ?? "local";
  const allowed: RuntimeEnvironment[] = ["local", "test", "development", "staging", "production"];

  if (!allowed.includes(environment as RuntimeEnvironment)) {
    throw new Error(`Invalid MARKETING_OPS_ENV: ${environment}`);
  }

  return environment as RuntimeEnvironment;
}

export function createServiceConfig(input: {
  readonly environment?: string | undefined;
  readonly host?: string | undefined;
  readonly port?: string | undefined;
  readonly fallbackPort: number;
  readonly serviceName: string;
}): ServiceConfig {
  return {
    environment: parseEnvironment(input.environment),
    host: input.host?.trim() || "0.0.0.0",
    port: parsePort(input.port, input.fallbackPort),
    serviceName: input.serviceName,
  };
}
