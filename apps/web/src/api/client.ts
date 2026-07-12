/**
 * API client (TEN-012): thin fetch wrapper over the control-plane API.
 *
 * Development identity: when a dev user is selected (stored locally), the
 * X-Dev-User header rides on every request — the server only honors it in
 * development/test environments (TEN-002).
 */

import { env } from "../env";

export const DEV_USER_STORAGE_KEY = "soa.dev.user";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export function getDevUser(): string | null {
  return globalThis.localStorage?.getItem(DEV_USER_STORAGE_KEY) ?? null;
}

export function setDevUser(value: string | null): void {
  if (value === null) {
    globalThis.localStorage?.removeItem(DEV_USER_STORAGE_KEY);
  } else {
    globalThis.localStorage?.setItem(DEV_USER_STORAGE_KEY, value);
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body) {
    headers.set("Content-Type", "application/json");
  }
  const devUser = getDevUser();
  if (devUser) {
    headers.set("X-Dev-User", devUser);
  }
  const response = await fetch(`${env.VITE_API_BASE_URL}${path}`, { ...init, headers });
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = (await response.json()) as { error?: { message?: string } };
      message = body.error?.message ?? message;
    } catch {
      // Non-JSON error body: keep the generic message.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

// ---------------------------------------------------------------------------
// Typed endpoints
// ---------------------------------------------------------------------------

export interface MembershipSummary {
  membership_id: string;
  organization_id: string;
  organization_slug: string;
  organization_name: string;
  organization_status: string;
  status: string;
  permissions: string[];
}

export interface MeResponse {
  user_id: string;
  email: string;
  display_name: string | null;
  auth_method: string;
  dev_session: boolean;
  memberships: MembershipSummary[];
}

export function fetchMe(): Promise<MeResponse> {
  return apiFetch<MeResponse>("/me");
}
