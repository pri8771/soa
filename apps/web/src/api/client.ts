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

// --- Jobs (JOB-006/007) ---

export interface JobSummary {
  id: string;
  job_type: string;
  status: "pending" | "running" | "succeeded" | "dead_letter" | "cancelled";
  priority: number;
  attempts: number;
  max_attempts: number;
  run_after: string;
  created_at: string;
  finished_at: string | null;
  last_error: string | null;
  correlation_id: string | null;
}

export interface JobsPage {
  items: JobSummary[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface QueueStats {
  by_status: Record<string, number>;
  oldest_pending_run_after: string | null;
}

export function fetchJobs(
  organizationSlug: string,
  options: { status?: string; cursor?: string } = {},
): Promise<JobsPage> {
  const params = new URLSearchParams();
  if (options.status) params.set("job_status", options.status);
  if (options.cursor) params.set("cursor", options.cursor);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return apiFetch<JobsPage>(`/orgs/${organizationSlug}/jobs${query}`);
}

export function fetchJobStats(organizationSlug: string): Promise<QueueStats> {
  return apiFetch<QueueStats>(`/orgs/${organizationSlug}/jobs/stats`);
}

export function replayJob(
  organizationSlug: string,
  jobId: string,
  reason: string,
): Promise<JobSummary> {
  return apiFetch<JobSummary>(`/orgs/${organizationSlug}/jobs/${jobId}/replay`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function cancelJob(
  organizationSlug: string,
  jobId: string,
  reason: string,
): Promise<JobSummary> {
  return apiFetch<JobSummary>(`/orgs/${organizationSlug}/jobs/${jobId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// --- Processes (CFG-008/009) ---

export interface ProcessSummary {
  id: string;
  name: string;
  slug: string;
  status: "active" | "archived";
  active_version_id: string | null;
  active_version_number: number | null;
  streams_count: number;
  draft_count: number;
  version: number;
}

export function fetchProcesses(organizationSlug: string): Promise<ProcessSummary[]> {
  return apiFetch<ProcessSummary[]>(`/orgs/${organizationSlug}/processes`);
}

// --- Streams (CFG-008/010) ---

export interface StreamSummary {
  id: string;
  process_id: string;
  name: string;
  slug: string;
  status: "active" | "paused" | "archived";
  active_version_id: string | null;
  process_name: string;
  process_slug: string;
  active_version_number: number | null;
}

export interface StreamVersionSummary {
  id: string;
  version_number: number;
  state: "draft" | "published" | "superseded";
  overrides: Record<string, unknown>;
  resolved_snapshot: {
    process_version_id: string;
    process_version_number: number;
    config: Record<string, unknown>;
  } | null;
  pinned_process_version_id: string | null;
  version: number;
}

export interface StreamDetail {
  stream: Omit<StreamSummary, "process_name" | "process_slug" | "active_version_number">;
  versions: StreamVersionSummary[];
}

export function fetchStreams(organizationSlug: string): Promise<StreamSummary[]> {
  return apiFetch<StreamSummary[]>(`/orgs/${organizationSlug}/streams`);
}

export function fetchStreamDetail(
  organizationSlug: string,
  streamSlug: string,
): Promise<StreamDetail> {
  return apiFetch<StreamDetail>(`/orgs/${organizationSlug}/streams/${streamSlug}`);
}

export function archiveStream(
  organizationSlug: string,
  streamSlug: string,
  impact: string,
): Promise<unknown> {
  return apiFetch(`/orgs/${organizationSlug}/streams/${streamSlug}/archive`, {
    method: "POST",
    body: JSON.stringify({ impact }),
  });
}

// --- Extraction schema (CFG-003/011) ---

export interface SchemaField {
  key: string;
  label: string;
  type: "text" | "number" | "money" | "date" | "boolean" | "enum" | "table";
  required?: boolean;
  criticality?: "critical" | "standard" | "informational";
  enum_values?: string[];
  columns?: SchemaField[];
}

export interface SchemaDefinition {
  fields: SchemaField[];
}

export interface SchemaVersionRecord {
  id: string;
  version_number: number;
  state: "draft" | "published" | "superseded";
  definition: SchemaDefinition;
  change_summary: string | null;
  version: number;
}

export interface SchemaListing {
  versions: SchemaVersionRecord[];
  published_json_schema: Record<string, unknown> | null;
}

export function fetchSchema(organizationSlug: string, processSlug: string): Promise<SchemaListing> {
  return apiFetch<SchemaListing>(`/orgs/${organizationSlug}/processes/${processSlug}/schema`);
}

export function createSchemaDraft(
  organizationSlug: string,
  processSlug: string,
  definition: SchemaDefinition,
): Promise<SchemaVersionRecord> {
  return apiFetch<SchemaVersionRecord>(
    `/orgs/${organizationSlug}/processes/${processSlug}/schema/versions`,
    { method: "POST", body: JSON.stringify({ definition }) },
  );
}

export function updateSchemaDraft(
  organizationSlug: string,
  processSlug: string,
  versionId: string,
  recordVersion: number,
  definition: SchemaDefinition,
): Promise<SchemaVersionRecord> {
  return apiFetch<SchemaVersionRecord>(
    `/orgs/${organizationSlug}/processes/${processSlug}/schema/versions/${versionId}`,
    {
      method: "PATCH",
      headers: { "If-Match": String(recordVersion) },
      body: JSON.stringify({ definition }),
    },
  );
}

export function publishSchemaVersion(
  organizationSlug: string,
  processSlug: string,
  versionId: string,
): Promise<SchemaVersionRecord> {
  return apiFetch<SchemaVersionRecord>(
    `/orgs/${organizationSlug}/processes/${processSlug}/schema/versions/${versionId}/publish`,
    { method: "POST" },
  );
}

// --- Validation rules (CFG-004/012) ---

export type RuleCondition =
  | { op: "field"; key: string }
  | { op: "const"; value: string | number | boolean }
  | { op: "eq" | "ne" | "gt" | "gte" | "lt" | "lte"; left: RuleCondition; right: RuleCondition }
  | { op: "and" | "or"; args: RuleCondition[] }
  | { op: "not"; arg: RuleCondition }
  | { op: "is_present"; key: string };

export interface RuleTestCase {
  values: Record<string, unknown>;
  expect_triggered: boolean;
}

export interface RuleRecord {
  key: string;
  severity: "error" | "warning" | "info";
  action: "block" | "route_to_review" | "annotate";
  condition: RuleCondition;
  test_cases?: RuleTestCase[];
}

export interface RuleSetDefinition {
  rules: RuleRecord[];
}

export interface RuleSetVersionRecord {
  id: string;
  version_number: number;
  state: "draft" | "published" | "superseded";
  definition: RuleSetDefinition;
  change_summary: string | null;
  version: number;
}

export interface RuleSetListing {
  versions: RuleSetVersionRecord[];
  field_types: Record<string, string>;
}

export function fetchRuleSet(
  organizationSlug: string,
  processSlug: string,
): Promise<RuleSetListing> {
  return apiFetch<RuleSetListing>(`/orgs/${organizationSlug}/processes/${processSlug}/rules`);
}

export function validateRuleSet(
  organizationSlug: string,
  processSlug: string,
  definition: RuleSetDefinition,
): Promise<{ valid: boolean; message: string | null }> {
  return apiFetch(`/orgs/${organizationSlug}/processes/${processSlug}/rules/validate`, {
    method: "POST",
    body: JSON.stringify({ definition }),
  });
}

export function createRuleSetDraft(
  organizationSlug: string,
  processSlug: string,
  definition: RuleSetDefinition,
): Promise<RuleSetVersionRecord> {
  return apiFetch<RuleSetVersionRecord>(
    `/orgs/${organizationSlug}/processes/${processSlug}/rules/versions`,
    { method: "POST", body: JSON.stringify({ definition }) },
  );
}

export function updateRuleSetDraft(
  organizationSlug: string,
  processSlug: string,
  versionId: string,
  recordVersion: number,
  definition: RuleSetDefinition,
): Promise<RuleSetVersionRecord> {
  return apiFetch<RuleSetVersionRecord>(
    `/orgs/${organizationSlug}/processes/${processSlug}/rules/versions/${versionId}`,
    {
      method: "PATCH",
      headers: { "If-Match": String(recordVersion) },
      body: JSON.stringify({ definition }),
    },
  );
}

export function publishRuleSetVersion(
  organizationSlug: string,
  processSlug: string,
  versionId: string,
): Promise<RuleSetVersionRecord> {
  return apiFetch<RuleSetVersionRecord>(
    `/orgs/${organizationSlug}/processes/${processSlug}/rules/versions/${versionId}/publish`,
    { method: "POST" },
  );
}

// --- Stream configuration inheritance (CFG-006/013) ---

export interface ResolvedValueEntry {
  value: unknown;
  source: "environment" | "process" | "stream";
}

export interface ResolvePreview {
  layers: {
    environment: Record<string, unknown>;
    process: Record<string, unknown>;
    stream: Record<string, unknown>;
  };
  resolved: {
    process_version_id: string;
    process_version_number: number;
    policies: Record<string, unknown>[];
    values: Record<string, ResolvedValueEntry>;
    fingerprint: string;
  };
}

export function resolveStreamPreview(
  organizationSlug: string,
  streamSlug: string,
  overrides: Record<string, unknown>,
): Promise<ResolvePreview> {
  return apiFetch<ResolvePreview>(`/orgs/${organizationSlug}/streams/${streamSlug}/resolve`, {
    method: "POST",
    body: JSON.stringify({ overrides }),
  });
}

export function createStreamVersion(
  organizationSlug: string,
  streamSlug: string,
  overrides: Record<string, unknown>,
): Promise<StreamVersionSummary> {
  return apiFetch<StreamVersionSummary>(
    `/orgs/${organizationSlug}/streams/${streamSlug}/versions`,
    {
      method: "POST",
      body: JSON.stringify({ overrides }),
    },
  );
}

export function updateStreamVersion(
  organizationSlug: string,
  streamSlug: string,
  versionId: string,
  recordVersion: number,
  overrides: Record<string, unknown>,
): Promise<StreamVersionSummary> {
  return apiFetch<StreamVersionSummary>(
    `/orgs/${organizationSlug}/streams/${streamSlug}/versions/${versionId}`,
    {
      method: "PATCH",
      headers: { "If-Match": String(recordVersion) },
      body: JSON.stringify({ overrides }),
    },
  );
}

export function publishStreamVersion(
  organizationSlug: string,
  streamSlug: string,
  versionId: string,
): Promise<StreamVersionSummary> {
  return apiFetch<StreamVersionSummary>(
    `/orgs/${organizationSlug}/streams/${streamSlug}/versions/${versionId}/publish`,
    { method: "POST" },
  );
}
