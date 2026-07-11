export const serviceNames = ["api", "worker"] as const;

export type ServiceName = (typeof serviceNames)[number];
export type HealthState = "ok" | "degraded";

export interface HealthResponse {
  readonly checkedAt: string;
  readonly service: ServiceName;
  readonly status: HealthState;
  readonly version: string;
}

export function buildHealthResponse(input: {
  readonly service: ServiceName;
  readonly status?: HealthState;
  readonly version?: string;
  readonly now?: Date;
}): HealthResponse {
  return {
    checkedAt: (input.now ?? new Date()).toISOString(),
    service: input.service,
    status: input.status ?? "ok",
    version: input.version ?? "0.1.0",
  };
}
