/**
 * API client (TEN-012): thin fetch wrapper over the control-plane API.
 *
 * Development identity: when a dev user is selected (stored locally), the
 * X-Dev-User header rides on requests only in Vite's development mode. Every
 * other browser build uses a production-shaped bearer session (TEN-002/003).
 */

import { expireAuthSession, forceRefreshBearerToken, getBearerToken } from "../auth/runtime";
import { env } from "../env";
import type { CanonicalOrder } from "./canonical-order";

export const DEV_USER_STORAGE_KEY = "soa.dev.user";

export interface ApiValidationDetail {
  location: string[];
  message: string;
  type: string;
  [key: string]: unknown;
}

export interface ApiErrorMetadata {
  code?: string;
  correlationId?: string;
  details?: ApiValidationDetail[];
  retryAfter?: string;
  retryAfterSeconds?: number;
}

export class ApiError extends Error {
  public readonly code: string | null;
  public readonly correlationId: string | null;
  public readonly details: readonly ApiValidationDetail[];
  public readonly retryAfter: string | null;
  public readonly retryAfterSeconds: number | null;

  constructor(
    public readonly status: number,
    message: string,
    metadata: ApiErrorMetadata = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.code = metadata.code ?? null;
    this.correlationId = metadata.correlationId ?? null;
    this.details = metadata.details ?? [];
    this.retryAfter = metadata.retryAfter ?? null;
    this.retryAfterSeconds = metadata.retryAfterSeconds ?? null;
  }
}

export function getDevUser(): string | null {
  if (env.MODE !== "development" || env.VITE_AUTH_MODE !== undefined) return null;
  return globalThis.localStorage?.getItem(DEV_USER_STORAGE_KEY) ?? null;
}

export function setDevUser(value: string | null): void {
  if (env.MODE !== "development" || env.VITE_AUTH_MODE !== undefined) {
    globalThis.localStorage?.removeItem(DEV_USER_STORAGE_KEY);
    return;
  }
  if (value === null) {
    globalThis.localStorage?.removeItem(DEV_USER_STORAGE_KEY);
  } else {
    globalThis.localStorage?.setItem(DEV_USER_STORAGE_KEY, value);
  }
}

function retryDelay(header: string | null): number | undefined {
  if (!header) return undefined;
  const seconds = Number(header);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds);
  const date = Date.parse(header);
  if (Number.isNaN(date)) return undefined;
  return Math.max(0, Math.ceil((date - Date.now()) / 1_000));
}

async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  let body: {
    error?: {
      code?: unknown;
      message?: unknown;
      correlation_id?: unknown;
      details?: unknown;
    };
  } = {};
  try {
    body = (await response.json()) as typeof body;
  } catch {
    // Non-JSON error body: retain HTTP and response-header metadata.
  }
  const error = body.error;
  const retryAfter = response.headers.get("Retry-After") ?? undefined;
  const details = Array.isArray(error?.details)
    ? error.details.filter(
        (detail): detail is ApiValidationDetail =>
          Boolean(detail) &&
          typeof detail === "object" &&
          Array.isArray((detail as ApiValidationDetail).location) &&
          typeof (detail as ApiValidationDetail).message === "string" &&
          typeof (detail as ApiValidationDetail).type === "string",
      )
    : undefined;
  return new ApiError(
    response.status,
    typeof error?.message === "string" ? error.message : `Request failed (${response.status}).`,
    {
      code: typeof error?.code === "string" ? error.code : undefined,
      correlationId:
        typeof error?.correlation_id === "string"
          ? error.correlation_id
          : (response.headers.get("X-Request-ID") ?? undefined),
      details,
      retryAfter,
      retryAfterSeconds: retryDelay(retryAfter ?? null),
    },
  );
}

/** Exported for the SSE stream (eventStream.ts): fetch-based streaming
 * needs the exact same auth headers as apiFetch, but EventSource cannot
 * carry them at all, so it builds its own fetch() call by hand. */
export function requestHeaders(init: RequestInit | undefined, bearerToken: string | null): Headers {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  if (init?.body && typeof init.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (bearerToken) headers.set("Authorization", `Bearer ${bearerToken}`);
  if (env.MODE === "development" && env.VITE_AUTH_MODE === undefined) {
    const devUser = getDevUser();
    if (devUser) headers.set("X-Dev-User", devUser);
  } else {
    // Never let a shared helper or caller leak the development bypass header
    // into a production-shaped browser request.
    headers.delete("X-Dev-User");
  }
  return headers;
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const explicitAuthorization = new Headers(init?.headers).has("Authorization");
  let bearerToken = explicitAuthorization ? null : await getBearerToken();
  let response = await fetch(`${env.VITE_API_BASE_URL}${path}`, {
    ...init,
    headers: requestHeaders(init, bearerToken),
  });

  // A rejected bearer is refreshed once. There is no recursive retry, and a
  // second 401 always destroys the local session before the error propagates.
  if (response.status === 401 && bearerToken) {
    try {
      bearerToken = await forceRefreshBearerToken();
      response = await fetch(`${env.VITE_API_BASE_URL}${path}`, {
        ...init,
        headers: requestHeaders(init, bearerToken),
      });
    } catch {
      expireAuthSession();
    }
  }

  if (!response.ok) {
    if (response.status === 401) expireAuthSession();
    throw await apiErrorFromResponse(response);
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

// --- Organizations, invitations, members, roles, and exports (TEN-008/SEC-009) ---

export interface OrganizationRecord {
  id: string;
  name: string;
  slug: string;
  status: string;
  version: number;
}

export interface OrganizationMember {
  membership_id: string;
  user_id: string | null;
  invited_email: string;
  status: "invited" | "active" | "suspended" | "removed";
  version: number;
  assigned_roles: Array<Pick<OrganizationRole, "id" | "name" | "slug" | "is_system">>;
}

export interface OrganizationRole {
  id: string;
  name: string;
  slug: string;
  is_system: boolean;
  permissions: string[];
}

export interface DataExportJob {
  id: string;
  scope: string;
  snapshot_at: string;
  state: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  total_documents: number;
  processed_documents: number;
  progress: number;
  total_records: number;
  safe_error: string | null;
  expires_at: string;
  manifest_download_url: string | null;
  manifest_expires_at: string | null;
  parts: Array<{
    name?: string;
    category?: string;
    download_url?: string;
    download_expires_at?: string;
  }>;
}

export function createOrganization(name: string, slug: string): Promise<OrganizationRecord> {
  return apiFetch("/organizations", {
    method: "POST",
    body: JSON.stringify({ name, slug }),
  });
}

export function acceptInvitation(organizationSlug: string): Promise<OrganizationMember> {
  return apiFetch("/invitations/accept", {
    method: "POST",
    body: JSON.stringify({ organization_slug: organizationSlug }),
  });
}

export function fetchOrganizationMembers(
  organizationSlug: string,
): Promise<{ items: OrganizationMember[]; has_more: boolean; next_cursor: string | null }> {
  return apiFetch(`/orgs/${organizationSlug}/members`);
}

export function inviteOrganizationMember(
  organizationSlug: string,
  email: string,
): Promise<{ membership_id: string; email: string; status: string; created: boolean }> {
  return apiFetch(`/orgs/${organizationSlug}/invitations`, {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export function changeOrganizationMemberStatus(
  organizationSlug: string,
  member: Pick<OrganizationMember, "membership_id" | "version">,
  status: "active" | "suspended" | "removed",
): Promise<OrganizationMember> {
  return apiFetch(`/orgs/${organizationSlug}/members/${member.membership_id}`, {
    method: "PATCH",
    headers: { "If-Match": String(member.version) },
    body: JSON.stringify({ status }),
  });
}

export function fetchOrganizationRoles(organizationSlug: string): Promise<OrganizationRole[]> {
  return apiFetch(`/orgs/${organizationSlug}/roles`);
}

export function createOrganizationRole(
  organizationSlug: string,
  body: { name: string; slug: string; permissions: string[] },
): Promise<OrganizationRole> {
  return apiFetch(`/orgs/${organizationSlug}/roles`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function assignOrganizationRole(
  organizationSlug: string,
  membershipId: string,
  roleSlug: string,
): Promise<{ membership_id: string; role_slug: string }> {
  return apiFetch(`/orgs/${organizationSlug}/members/${membershipId}/roles`, {
    method: "POST",
    body: JSON.stringify({ role_slug: roleSlug }),
  });
}

export function fetchOrganizationMemberRoles(
  organizationSlug: string,
  membershipId: string,
): Promise<OrganizationRole[]> {
  return apiFetch(`/orgs/${organizationSlug}/members/${membershipId}/roles`);
}

export function revokeOrganizationRole(
  organizationSlug: string,
  membershipId: string,
  roleSlug: string,
): Promise<{ membership_id: string; role_slug: string; revoked: boolean }> {
  return apiFetch(`/orgs/${organizationSlug}/members/${membershipId}/roles/${roleSlug}`, {
    method: "DELETE",
  });
}

export function fetchOrganizationDataExports(
  organizationSlug: string,
): Promise<{ items: DataExportJob[] }> {
  return apiFetch(`/orgs/${organizationSlug}/data-exports`);
}

export function createOrganizationDataExport(
  organizationSlug: string,
  idempotencyKey = crypto.randomUUID(),
): Promise<DataExportJob> {
  return apiFetch(`/orgs/${organizationSlug}/data-exports`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
  });
}

export function cancelOrganizationDataExport(
  organizationSlug: string,
  exportId: string,
  reason: string,
): Promise<DataExportJob> {
  return apiFetch(`/orgs/${organizationSlug}/data-exports/${exportId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
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
  last_claim_at: string | null;
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

// --- Classifier routing tables (intake → skill routing) ---

export interface ClassifierRoute {
  label: string;
  target_stream_id: string;
  signals: string[];
}

export interface ClassifierContent {
  routes: ClassifierRoute[];
}

export interface ClassifierVersion {
  id: string;
  stream_id: string;
  version_number: number;
  state: "draft" | "published" | "superseded";
  reference: string;
  content: ClassifierContent;
  change_summary: string | null;
  published_at: string | null;
  published_by: string | null;
}

export function fetchClassifier(
  organizationSlug: string,
  streamSlug: string,
): Promise<{ items: ClassifierVersion[] }> {
  return apiFetch(`/orgs/${organizationSlug}/streams/${streamSlug}/classifier`);
}

export function createClassifierDraft(
  organizationSlug: string,
  streamSlug: string,
  content: ClassifierContent,
  changeSummary?: string | null,
): Promise<ClassifierVersion> {
  return apiFetch(`/orgs/${organizationSlug}/streams/${streamSlug}/classifier`, {
    method: "POST",
    body: JSON.stringify({ content, change_summary: changeSummary ?? null }),
  });
}

export function updateClassifierDraft(
  organizationSlug: string,
  versionId: string,
  content: ClassifierContent,
  changeSummary?: string | null,
): Promise<ClassifierVersion> {
  return apiFetch(`/orgs/${organizationSlug}/classifier-versions/${versionId}`, {
    method: "PATCH",
    body: JSON.stringify({ content, change_summary: changeSummary ?? null }),
  });
}

export function publishClassifierVersion(
  organizationSlug: string,
  versionId: string,
): Promise<ClassifierVersion> {
  return apiFetch(`/orgs/${organizationSlug}/classifier-versions/${versionId}/publish`, {
    method: "POST",
  });
}

// --- Versioned extraction instructions (AIO-010) ---

export interface InstructionVersion {
  id: string;
  stream_version_id: string;
  schema_version_id: string;
  version_number: number;
  state: "draft" | "published" | "superseded";
  reference: string;
  content: { instructions: string; field_guidance: Record<string, string> };
  change_summary: string | null;
  published_at: string | null;
  published_by: string | null;
}

export function fetchInstructionVersions(
  organizationSlug: string,
  streamVersionId: string,
): Promise<{ items: InstructionVersion[] }> {
  return apiFetch(`/orgs/${organizationSlug}/stream-versions/${streamVersionId}/instructions`);
}

export function createInstructionDraft(
  organizationSlug: string,
  streamVersionId: string,
  schemaVersionId: string,
  content: InstructionVersion["content"],
  changeSummary: string | null,
): Promise<InstructionVersion> {
  return apiFetch(`/orgs/${organizationSlug}/stream-versions/${streamVersionId}/instructions`, {
    method: "POST",
    body: JSON.stringify({
      schema_version_id: schemaVersionId,
      content,
      change_summary: changeSummary,
    }),
  });
}

export function updateInstructionDraft(
  organizationSlug: string,
  instructionId: string,
  content: InstructionVersion["content"],
  changeSummary: string | null,
): Promise<InstructionVersion> {
  return apiFetch(`/orgs/${organizationSlug}/instructions/${instructionId}`, {
    method: "PATCH",
    body: JSON.stringify({ content, change_summary: changeSummary }),
  });
}

export function publishInstructionVersion(
  organizationSlug: string,
  instructionId: string,
): Promise<InstructionVersion> {
  return apiFetch(`/orgs/${organizationSlug}/instructions/${instructionId}/publish`, {
    method: "POST",
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

// --- Process versions: timeline, diff, rollback (CFG-007/014) ---

export interface ProcessVersionSummary {
  id: string;
  version_number: number;
  state: "draft" | "published" | "superseded";
  change_summary: string | null;
  published_at: string | null;
  published_by: string | null;
  version: number;
}

export interface ProcessVersionDetail extends ProcessVersionSummary {
  definition: Record<string, unknown>;
}

export interface ProcessDetail {
  process: {
    id: string;
    name: string;
    slug: string;
    status: "active" | "archived";
    active_version_id: string | null;
    version: number;
  };
  versions: ProcessVersionSummary[];
}

export function fetchProcessDetail(
  organizationSlug: string,
  processSlug: string,
): Promise<ProcessDetail> {
  return apiFetch<ProcessDetail>(`/orgs/${organizationSlug}/processes/${processSlug}`);
}

export function fetchProcessVersion(
  organizationSlug: string,
  processSlug: string,
  versionId: string,
): Promise<ProcessVersionDetail> {
  return apiFetch<ProcessVersionDetail>(
    `/orgs/${organizationSlug}/processes/${processSlug}/versions/${versionId}`,
  );
}

export function rollbackProcess(
  organizationSlug: string,
  processSlug: string,
  targetVersionId: string,
  reason: string,
): Promise<ProcessDetail["process"]> {
  return apiFetch<ProcessDetail["process"]>(
    `/orgs/${organizationSlug}/processes/${processSlug}/rollback`,
    { method: "POST", body: JSON.stringify({ target_version_id: targetVersionId, reason }) },
  );
}

// --- Upload sessions (ING-002/008) ---

export interface UploadSessionCreated {
  session_id: string;
  document_id: string;
  upload_url: string;
  upload_method: string;
  upload_headers: Record<string, string>;
  expires_at: string;
  state: string;
}

export interface UploadCompleteResult {
  document_id: string;
  state: string;
  state_reason: string | null;
  duplicate_of: string | null;
}

export function createUploadSession(
  organizationSlug: string,
  streamSlug: string,
  declaration: {
    filename: string;
    content_type: string;
    size_bytes: number;
    sha256: string;
    client_reference?: string;
  },
): Promise<UploadSessionCreated> {
  return apiFetch<UploadSessionCreated>(`/orgs/${organizationSlug}/streams/${streamSlug}/uploads`, {
    method: "POST",
    body: JSON.stringify(declaration),
  });
}

export function completeUploadSession(
  organizationSlug: string,
  sessionId: string,
): Promise<UploadCompleteResult> {
  return apiFetch<UploadCompleteResult>(`/orgs/${organizationSlug}/uploads/${sessionId}/complete`, {
    method: "POST",
  });
}

export function abortUploadSession(
  organizationSlug: string,
  sessionId: string,
): Promise<{ state: string }> {
  return apiFetch<{ state: string }>(`/orgs/${organizationSlug}/uploads/${sessionId}/abort`, {
    method: "POST",
  });
}

/** Blob bytes with a FileReader fallback (jsdom Files lack arrayBuffer). */
export function readBlobBytes(file: Blob): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === "function") return file.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(reader.error ?? new Error("File could not be read."));
    reader.readAsArrayBuffer(file);
  });
}

/** SHA-256 of a browser File/Blob, hex-encoded. */
export async function sha256OfFile(file: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await readBlobBytes(file));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// --- Documents queue (ING-009/010) ---

export interface DocumentSummary {
  id: string;
  stream_id: string;
  state: string;
  state_reason: string | null;
  source_channel: string;
  original_filename: string;
  content_sha256: string;
  size_bytes: number;
  content_type: string;
  client_reference: string | null;
  priority: number;
  sla_due_at: string | null;
  received_at: string;
  duplicate_of: string | null;
}

export interface DocumentsPage {
  items: DocumentSummary[];
  has_more: boolean;
  next_cursor: string | null;
}

export function fetchDocuments(
  organizationSlug: string,
  options: {
    state?: string;
    stream?: string;
    channel?: string;
    search?: string;
    cursor?: string;
  } = {},
): Promise<DocumentsPage> {
  const params = new URLSearchParams();
  if (options.state) params.set("document_state", options.state);
  if (options.stream) params.set("stream", options.stream);
  if (options.channel) params.set("source_channel", options.channel);
  if (options.search) params.set("search", options.search);
  if (options.cursor) params.set("cursor", options.cursor);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return apiFetch<DocumentsPage>(`/orgs/${organizationSlug}/documents${query}`);
}

export function cancelDocument(
  organizationSlug: string,
  documentId: string,
  reason: string,
): Promise<{ id: string; state: string }> {
  return apiFetch(`/orgs/${organizationSlug}/documents/${documentId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// --- Approval-gated document data deletion (SEC-010) ---

export interface DocumentDeletionRequest {
  id: string;
  document_id: string;
  state: "pending_approval" | "approved" | "running" | "failed" | "completed" | "cancelled";
  reason: string;
  requested_by: string;
  requested_at: string;
  approved_by: string | null;
  approval_reason: string | null;
  approved_at: string | null;
  completed_at: string | null;
  cancelled_by: string | null;
  cancellation_reason: string | null;
  cancelled_at: string | null;
  safe_error: string | null;
  version: number;
}

export interface DocumentDeletionRequests {
  items: DocumentDeletionRequest[];
}

/** Request erasure. A different principal must approve before the durable
 * worker can delete any object or row; legal holds remain an absolute veto. */
export function requestDocumentDeletion(
  organizationSlug: string,
  documentId: string,
  reason: string,
): Promise<DocumentDeletionRequest> {
  return apiFetch(`/orgs/${organizationSlug}/documents/${documentId}/deletion-requests`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

/** Read the durable deletion lifecycle for one document. A missing lifecycle
 * is normal for settled documents and is represented as null, not an error. */
export async function fetchDocumentDeletionRequest(
  organizationSlug: string,
  documentId: string,
): Promise<DocumentDeletionRequest | null> {
  try {
    return await apiFetch<DocumentDeletionRequest>(
      `/orgs/${organizationSlug}/documents/${documentId}/deletion-request`,
    );
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export function fetchDocumentDeletionRequests(
  organizationSlug: string,
  limit = 200,
): Promise<DocumentDeletionRequests> {
  return apiFetch(`/orgs/${organizationSlug}/deletion-requests?limit=${limit}`);
}

export function approveDocumentDeletion(
  organizationSlug: string,
  requestId: string,
  reason: string,
): Promise<DocumentDeletionRequest> {
  return apiFetch(`/orgs/${organizationSlug}/deletion-requests/${requestId}/approve`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function cancelDocumentDeletionRequest(
  organizationSlug: string,
  requestId: string,
  reason: string,
): Promise<DocumentDeletionRequest> {
  return apiFetch(`/orgs/${organizationSlug}/deletion-requests/${requestId}/cancel`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export interface DocumentLegalHold {
  id: string;
  document_id: string;
  state: "active" | "released";
  reason: string;
  placed_by: string;
  placed_at: string;
  released_by: string | null;
  release_reason: string | null;
  released_at: string | null;
  version: number;
}

export function fetchDocumentLegalHolds(
  organizationSlug: string,
  documentId: string,
): Promise<{ items: DocumentLegalHold[] }> {
  return apiFetch(`/orgs/${organizationSlug}/documents/${documentId}/legal-holds`);
}

export function placeDocumentLegalHold(
  organizationSlug: string,
  documentId: string,
  reason: string,
): Promise<DocumentLegalHold> {
  return apiFetch(`/orgs/${organizationSlug}/documents/${documentId}/legal-holds`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function releaseDocumentLegalHold(
  organizationSlug: string,
  holdId: string,
  reason: string,
): Promise<DocumentLegalHold> {
  return apiFetch(`/orgs/${organizationSlug}/legal-holds/${holdId}/release`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// --- Document detail (ING-011/012) ---

export interface DocumentArtifact {
  id: string;
  kind: string;
  sha256: string;
  size_bytes: number;
  content_type: string;
  produced_by_stage: string | null;
  retention_class: string;
  created_at: string;
}

export interface TimelineEntry {
  occurred_at: string;
  action: string;
  actor_type: string;
  actor_id: string;
  target_type: string;
  summary: Record<string, unknown>;
  correlation_id: string | null;
}

export interface DocumentDetail {
  document: DocumentSummary;
  artifacts: DocumentArtifact[];
  context: {
    stream_id: string;
    stream_slug?: string;
    stream_name?: string;
    stream_version_number?: number;
    pinned_process_version_id?: string | null;
  };
  timeline: TimelineEntry[];
  deletion_tombstone?: {
    completed_at: string | null;
    object_keys_deleted: number | null;
    category_counts: Record<string, number>;
  };
}

function deletedDocumentTombstone(detail: DocumentDetail): DocumentDetail {
  const completion = [...detail.timeline]
    .reverse()
    .find(
      (entry) =>
        entry.action === "document.deletion_completed" || entry.action === "document.data_deleted",
    );
  const explicitTombstone = detail.deletion_tombstone;
  const rawCounts = explicitTombstone?.category_counts ?? completion?.summary["category_counts"];
  const categoryCounts =
    rawCounts && typeof rawCounts === "object" && !Array.isArray(rawCounts)
      ? Object.fromEntries(
          Object.entries(rawCounts).filter(
            (entry): entry is [string, number] =>
              typeof entry[1] === "number" && Number.isFinite(entry[1]) && entry[1] >= 0,
          ),
        )
      : {};
  const rawObjectCount =
    explicitTombstone?.object_keys_deleted ?? completion?.summary["object_keys_deleted"];
  const objectKeysDeleted =
    typeof rawObjectCount === "number" && Number.isFinite(rawObjectCount) && rawObjectCount >= 0
      ? rawObjectCount
      : null;

  // A deleted response can still carry retained document-shell and historical
  // audit fields. Reduce it before it reaches React Query so the browser never
  // caches filename, hash, stream, actor, reason, artifact, or timeline data.
  return {
    document: {
      id: detail.document.id,
      stream_id: "",
      state: "deleted",
      state_reason: "document data deleted",
      source_channel: "",
      original_filename: "[deleted]",
      content_sha256: "",
      size_bytes: 0,
      content_type: "application/octet-stream",
      client_reference: null,
      priority: 0,
      sla_due_at: null,
      received_at: explicitTombstone?.completed_at ?? completion?.occurred_at ?? "",
      duplicate_of: null,
    },
    artifacts: [],
    context: { stream_id: "" },
    timeline: [],
    deletion_tombstone: {
      completed_at: explicitTombstone?.completed_at ?? completion?.occurred_at ?? null,
      object_keys_deleted: objectKeysDeleted,
      category_counts: categoryCounts,
    },
  };
}

export async function fetchDocumentDetail(
  organizationSlug: string,
  documentId: string,
): Promise<DocumentDetail> {
  const detail = await apiFetch<DocumentDetail>(
    `/orgs/${organizationSlug}/documents/${documentId}`,
  );
  return detail.document.state === "deleted" ? deletedDocumentTombstone(detail) : detail;
}

export function requestArtifactDownload(
  organizationSlug: string,
  artifactId: string,
): Promise<{ url: string; expires_at: string; method: string }> {
  return apiFetch(`/orgs/${organizationSlug}/artifacts/${artifactId}/download-url`, {
    method: "POST",
  });
}

// --- Processing runs (PRC-014) ---

export interface StageRunEntry {
  stage: string;
  attempt: number;
  state: string;
  provider: string | null;
  latency_ms: number | null;
  cost_cents: number;
  safe_error: string | null;
  failure_class: string | null;
  output_summary: Record<string, unknown>;
  started_at: string;
  finished_at: string | null;
}

export interface ProcessingRunEntry {
  id: string;
  run_number: number;
  state: string;
  triggered_by: string;
  stream_version_id: string | null;
  config_fingerprint: string | null;
  contract_fingerprint?: string | null;
  runtime_fingerprint?: string | null;
  runtime_provenance?: Record<string, unknown> | null;
  started_at: string;
  finished_at: string | null;
  total_latency_ms: number;
  total_cost_cents: number;
  stages: StageRunEntry[];
}

export interface DocumentRuns {
  document_id: string;
  state: string;
  runs: ProcessingRunEntry[];
}

export function fetchDocumentRuns(
  organizationSlug: string,
  documentId: string,
): Promise<DocumentRuns> {
  return apiFetch<DocumentRuns>(`/orgs/${organizationSlug}/documents/${documentId}/runs`);
}

export interface ReprocessResult {
  id: string;
  state: string;
  mode: string;
  run_number: number;
  consequence: string;
}

export function reprocessDocument(
  organizationSlug: string,
  documentId: string,
  options: { mode: "retry" | "current_config" | "historical_config"; reason: string },
): Promise<ReprocessResult> {
  return apiFetch(`/orgs/${organizationSlug}/documents/${documentId}/reprocess`, {
    method: "POST",
    body: JSON.stringify(options),
  });
}

// --- Review queue (REV-002/003) ---

export interface ReviewReason {
  code: string;
  message: string;
  field_key: string | null;
  row_index: number | null;
  rule_key: string | null;
}

export interface ReviewTaskEntry {
  id: string;
  document_id: string;
  run_id: string;
  state: string;
  priority: number;
  blocking: boolean;
  sla_due_at: string | null;
  assigned_to: string | null;
  assigned_at: string | null;
  reasons: ReviewReason[];
  outcome: string | null;
  version: number;
  created_at: string;
  document_filename: string | null;
  document_state: string | null;
}

export interface ReviewTasksPage {
  items: ReviewTaskEntry[];
  has_more: boolean;
  next_cursor: string | null;
}

export function fetchReviewTasks(
  organizationSlug: string,
  options: { view?: string; sort?: string; cursor?: string } = {},
): Promise<ReviewTasksPage> {
  const params = new URLSearchParams();
  if (options.view && options.view !== "all") params.set("view", options.view);
  if (options.sort) params.set("sort", options.sort);
  if (options.cursor) params.set("cursor", options.cursor);
  const query = params.size > 0 ? `?${params.toString()}` : "";
  return apiFetch<ReviewTasksPage>(`/orgs/${organizationSlug}/review-tasks${query}`);
}

export function claimReviewTask(
  organizationSlug: string,
  taskId: string,
): Promise<ReviewTaskEntry> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/claim`, { method: "POST" });
}

export function claimNextReviewTask(
  organizationSlug: string,
): Promise<{ task: ReviewTaskEntry | null; explanation: string }> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/claim-next`, { method: "POST" });
}

export function releaseReviewTask(
  organizationSlug: string,
  taskId: string,
): Promise<ReviewTaskEntry> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/release`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

// --- Document pages (REV-004 viewer) ---

export interface DocumentPageEntry {
  page_number: number;
  width_px: number;
  height_px: number;
  dpi: number | null;
  rotation_degrees: number;
  content_type: string;
  image_artifact_id: string;
  text_artifact_id: string | null;
}

export interface DocumentPages {
  document_id: string;
  run_id: string | null;
  run_number: number | null;
  pages: DocumentPageEntry[];
}

export function fetchDocumentPages(
  organizationSlug: string,
  documentId: string,
): Promise<DocumentPages> {
  return apiFetch<DocumentPages>(`/orgs/${organizationSlug}/documents/${documentId}/pages`);
}

// --- Review workspace (REV-006/007/009) ---

export interface WorkspaceEvidence {
  page_number: number;
  certainty: "region" | "page";
  polygon: [number, number][] | null;
  quote: string | null;
}

export interface WorkspaceField {
  field_key: string;
  row_index: number | null;
  raw_value: string | null;
  normalized_value: unknown;
  normalization_error: string | null;
  confidence: number;
  validation_status: string;
  provider: string;
  provider_model: string | null;
  evidence: WorkspaceEvidence[];
  candidates: { raw_value: string; confidence: number }[];
}

export interface WorkspaceCorrection {
  field_key: string;
  row_index: number | null;
  corrected_raw_value: string | null;
  corrected_normalized_value: unknown;
  normalization_error: string | null;
  corrected_by: string;
}

export interface ReviewWorkspace {
  task: ReviewTaskEntry;
  /** When this task was superseded (e.g. a reprocess opened a fresh task
   * for the new run), the id of the document's current active task, so a
   * stale/bookmarked URL can redirect to the live review. */
  superseded_by_task_id: string | null;
  document: {
    id: string;
    state: string;
    state_reason: string | null;
    original_filename: string;
    priority: number;
    received_at: string;
  };
  run: {
    id: string;
    run_number: number | null;
    state: string | null;
    decision: { route: string; reasons: Record<string, unknown>[] } | null;
  };
  fields: WorkspaceField[];
  line_items: Record<string, WorkspaceField[][]>;
  corrections: WorkspaceCorrection[];
  pages: DocumentPageEntry[];
  history: Record<string, unknown>[];
  context: Record<string, unknown>;
}

export function fetchReviewWorkspace(
  organizationSlug: string,
  taskId: string,
): Promise<ReviewWorkspace> {
  return apiFetch<ReviewWorkspace>(`/orgs/${organizationSlug}/review-tasks/${taskId}/workspace`);
}

export interface CorrectionResult {
  correction: {
    id: string;
    field_key: string;
    row_index: number | null;
    previous_raw_value: string | null;
    corrected_raw_value: string | null;
    corrected_normalized_value: unknown;
    normalization_error: string | null;
    corrected_by: string;
  };
  task_version: number;
  revalidation: {
    evaluation: Record<string, unknown>;
    decision: { route: string; reasons: Record<string, unknown>[] };
  } | null;
}

/** A reviewer-supplied or auto-located evidence region for a corrected
 * field. `polygon` null = page-level (no exact region). */
export interface EvidenceSelection {
  page_number: number;
  polygon: number[][] | null;
  quote?: string | null;
}

export function correctField(
  organizationSlug: string,
  taskId: string,
  body: {
    field_key: string;
    row_index?: number | null;
    value: string | null;
    reason?: string;
    expected_version: number;
    evidence_selection?: EvidenceSelection | null;
  },
): Promise<CorrectionResult> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/corrections`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface LocateResult {
  found: boolean;
  page_number?: number;
  polygon?: number[][];
  reason?: string;
}

/** Find where a typed value sits on the page (AIO-014 at review time), so
 * the viewer can highlight it. `found: false` means draw the box by hand. */
export function locateFieldValue(
  organizationSlug: string,
  taskId: string,
  value: string,
  pageHint?: number,
): Promise<LocateResult> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/locate`, {
    method: "POST",
    body: JSON.stringify({ value, page_hint: pageHint ?? null }),
  });
}

// --- Review collaboration (REV-011) ---

export interface ReviewComment {
  id: string;
  task_id: string;
  document_id: string;
  author: string;
  body: string;
  mentions: string[];
  created_at: string;
}

export function fetchReviewComments(
  organizationSlug: string,
  taskId: string,
): Promise<{ items: ReviewComment[] }> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/comments`);
}

export function addReviewComment(
  organizationSlug: string,
  taskId: string,
  body: string,
): Promise<ReviewComment> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/comments`, {
    method: "POST",
    body: JSON.stringify({ body }),
  });
}

// --- Operations dashboard (ANA-004) ---

export interface MetricDefinitionEntry {
  key: string;
  description: string;
  numerator: string;
  denominator: string;
  timezone: string;
}

export interface AttentionItem {
  key: string;
  label: string;
  count: number;
  link: { screen: string; filters: Record<string, string> };
}

export interface OperationsSnapshot {
  window: { since: string; until: string; timezone: string };
  stream_id: string | null;
  volume: { per_day: { day: string; state: string; count: number }[] };
  latency: {
    runs_measured: number;
    sample_capped: boolean;
    avg_ms: number | null;
    p50_ms: number | null;
    p95_ms: number | null;
  };
  backlog: {
    documents_by_state: Record<string, number>;
    review_tasks: Record<string, number>;
  };
  exceptions: { documents_by_state: Record<string, number> };
  sla: {
    overdue_now: number;
    active_with_sla: number;
    breached_completed: number;
    completed_with_sla: number;
  };
  exports: {
    jobs_by_state: Record<string, number>;
    attempts_total: number;
    attempts_delivered: number;
  };
  notes: string[];
  definitions: Record<string, MetricDefinitionEntry>;
  needs_attention: AttentionItem[];
}

export function fetchOperationsSnapshot(organizationSlug: string): Promise<OperationsSnapshot> {
  return apiFetch(`/orgs/${organizationSlug}/analytics/operations`);
}

// --- Quality dashboard (ANA-005) ---

export interface QualitySnapshot {
  window: { since: string; until: string; timezone: string };
  reviewed: { tasks_completed: number; runs: number };
  field_corrections: {
    field_key: string;
    present_runs: number;
    corrected_runs: number;
    correction_rate: number | null;
  }[];
  line_corrections: {
    cells_present: number;
    cells_corrected: number;
    correction_rate: number | null;
  };
  stp: { settled_documents: number; straight_through: number; stp_rate: number | null };
  false_auto_approval: { available: boolean; reason: string };
  calibration: {
    cohorts: {
      confidence_range: string;
      fields_reviewed: number;
      corrected: number;
      correction_rate: number | null;
    }[];
  };
  ground_truth: { gold_documents: number; note: string };
  notes: string[];
  definitions: Record<string, MetricDefinitionEntry>;
}

export function fetchQualitySnapshot(organizationSlug: string): Promise<QualitySnapshot> {
  return apiFetch(`/orgs/${organizationSlug}/analytics/quality`);
}

// --- Audit trail (ANA-007) ---

export interface AuditEventEntry {
  id: string;
  occurred_at: string;
  actor_type: string;
  actor_id: string;
  action: string;
  target_type: string;
  target_id: string;
  summary: Record<string, unknown>;
}

export function fetchAuditEvents(
  organizationSlug: string,
  params: { action?: string; target_type?: string; cursor?: string },
): Promise<{ items: AuditEventEntry[]; has_more: boolean; next_cursor: string | null }> {
  const search = new URLSearchParams();
  if (params.action) search.set("action", params.action);
  if (params.target_type) search.set("target_type", params.target_type);
  if (params.cursor) search.set("cursor", params.cursor);
  const query = search.toString();
  return apiFetch(`/orgs/${organizationSlug}/audit-events${query ? `?${query}` : ""}`);
}

export interface AuditExportFile {
  name: string;
  sha256: string;
  bytes: number;
  events: number;
  download_url: string;
  expires_at: string;
}

export interface AuditExportResult {
  export_id: string;
  event_count: number;
  manifest_sha256: string;
  manifest_download_url: string;
  manifest_expires_at: string;
  files: AuditExportFile[];
}

export function createAuditExport(
  organizationSlug: string,
  filters: { action?: string; target_type?: string; since?: string; until?: string },
): Promise<AuditExportResult> {
  return apiFetch(`/orgs/${organizationSlug}/audit-exports`, {
    method: "POST",
    body: JSON.stringify(filters),
  });
}

// --- Cost dashboard (ANA-006) ---

export interface UsageGroup {
  stream_id: string | null;
  provider: string;
  provider_model: string | null;
  cost_category: string;
  billed_unit: string | null;
  billed_quantity: number | null;
  pages: number;
  entries: number;
  estimated_cents: number;
  adjustment_cents: number;
  reconciled_cents: number;
}

export interface UsageSnapshot {
  window: { since: string; until: string; timezone: string };
  groups: UsageGroup[];
  totals: { estimated_cents: number; adjustment_cents: number; reconciled_cents: number };
  semantics: Record<string, string>;
  quotas: {
    configured: boolean;
    reason?: string;
    key?: string;
    limit_cents?: number;
    spent_cents?: number;
    risk_ratio?: number | null;
    notes?: string[];
  };
}

export function fetchUsageSnapshot(organizationSlug: string): Promise<UsageSnapshot> {
  return apiFetch(`/orgs/${organizationSlug}/analytics/usage`);
}

// --- Catalog candidate matching in review (CAT-010) ---

export interface CatalogMatchFeature {
  name: string;
  score: number;
  explanation: string;
}

export interface CatalogMatchCandidate {
  id: string;
  code: string;
  label: string;
  score: number;
  features: CatalogMatchFeature[];
}

export interface CatalogCandidatesResponse {
  available: boolean;
  reason?: string | null;
  field_type?: string;
  outcome?: string;
  machine_selected_source_id?: string | null;
  reasons?: string[];
  candidates: CatalogMatchCandidate[];
  task_version?: number;
}

export function fetchCatalogCandidates(
  organizationSlug: string,
  taskId: string,
  fieldKey: string,
  q: string,
): Promise<CatalogCandidatesResponse> {
  const params = new URLSearchParams({ field_key: fieldKey, q });
  return apiFetch(
    `/orgs/${organizationSlug}/review-tasks/${taskId}/catalog-candidates?${params.toString()}`,
  );
}

export interface CatalogSelectionResult {
  correction: CorrectionResult["correction"] | null;
  task_version: number;
  override: boolean;
  revalidation: CorrectionResult["revalidation"];
}

export function postCatalogSelection(
  organizationSlug: string,
  taskId: string,
  body: {
    field_key: string;
    row_index: number | null;
    query: string;
    selected_source_id: string | null;
    reason?: string;
    expected_version: number;
  },
): Promise<CatalogSelectionResult> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/catalog-selection`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// --- Approval, rejection, escalation (REV-012/013, REV-011) ---

export interface ApprovalWarning {
  code: string | null;
  message?: string | null;
  field_key: string | null;
  row_index?: number | null;
  rule_key: string | null;
}

export interface ApprovalResult {
  status: "approved" | "pending_second_approval";
  idempotent: boolean;
  task_version: number;
  warnings: ApprovalWarning[];
  override_used: boolean;
  task: ReviewTaskEntry;
}

export function approveReviewTask(
  organizationSlug: string,
  taskId: string,
  overrideReason?: string,
): Promise<ApprovalResult> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/approve`, {
    method: "POST",
    body: JSON.stringify(overrideReason ? { override_reason: overrideReason } : {}),
  });
}

export interface RejectionResult {
  status: "rejected";
  idempotent: boolean;
  task_version: number;
  task: ReviewTaskEntry;
}

export function rejectReviewTask(
  organizationSlug: string,
  taskId: string,
  reason: string,
): Promise<RejectionResult> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// --- Integrations and mapping profiles (EXP-003/004) ---

export interface IntegrationEntry {
  id: string;
  name: string;
  slug: string;
  integration_type: string;
  status: "active" | "paused" | "archived";
  endpoint_url: string | null;
  credential_configured: boolean;
  production_ready: boolean;
  readiness_detail: string;
  idempotency_mechanism: string;
  active_mapping_version_id: string | null;
  version: number;
  created_at: string;
}

export interface MappingFieldSpec {
  target: string;
  source: string;
  format?: Record<string, unknown>;
  default?: unknown;
  when?: Record<string, unknown>;
  required?: boolean;
}

export interface MappingDefinition {
  fields?: MappingFieldSpec[];
  constants?: { target: string; value: unknown }[];
  lines?: { source: string; target: string; fields: MappingFieldSpec[] };
}

export interface MappingVersionRecord {
  id: string;
  integration_id: string;
  version_number: number;
  state: string;
  definition: MappingDefinition;
  target_schema: Record<string, unknown>;
  change_summary: string | null;
  published_at: string | null;
  published_by: string | null;
  version: number;
}

export interface IntegrationDetail {
  integration: IntegrationEntry;
  mapping_versions: MappingVersionRecord[];
}

export interface MappingValidationResult {
  valid: boolean;
  errors: string[];
  payload: Record<string, unknown> | null;
  trace: Record<string, unknown>[];
  notes?: string[];
}

export function fetchIntegrations(organizationSlug: string): Promise<{
  items: IntegrationEntry[];
}> {
  return apiFetch(`/orgs/${organizationSlug}/integrations`);
}

export function createIntegration(
  organizationSlug: string,
  body: {
    name: string;
    slug: string;
    integration_type: string;
    endpoint_url: string | null;
  },
): Promise<IntegrationEntry> {
  return apiFetch(`/orgs/${organizationSlug}/integrations`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function setIntegrationCredential(
  organizationSlug: string,
  integrationSlug: string,
  kind: string,
  secret: string,
): Promise<{ credential_configured: true; kind: string; rotated: boolean }> {
  return apiFetch(`/orgs/${organizationSlug}/integrations/${integrationSlug}/credential`, {
    method: "PUT",
    body: JSON.stringify({ kind, secret }),
  });
}

export interface IntegrationConnectionResult {
  ok: boolean;
  detail: string;
}

export interface IntegrationActivationResult {
  activated: boolean;
  detail: string;
  integration: IntegrationEntry;
}

export function testIntegrationConnection(
  organizationSlug: string,
  integrationSlug: string,
): Promise<IntegrationConnectionResult> {
  return apiFetch(`/orgs/${organizationSlug}/integrations/${integrationSlug}/connection-test`, {
    method: "POST",
  });
}

export function activateIntegration(
  organizationSlug: string,
  integration: Pick<IntegrationEntry, "slug" | "version">,
): Promise<IntegrationActivationResult> {
  return apiFetch(`/orgs/${organizationSlug}/integrations/${integration.slug}/activate`, {
    method: "POST",
    headers: { "If-Match": String(integration.version) },
  });
}

export function deactivateIntegration(
  organizationSlug: string,
  integration: Pick<IntegrationEntry, "slug" | "version">,
): Promise<IntegrationEntry> {
  return apiFetch(`/orgs/${organizationSlug}/integrations/${integration.slug}/deactivate`, {
    method: "POST",
    headers: { "If-Match": String(integration.version) },
  });
}

export function archiveIntegration(
  organizationSlug: string,
  integration: Pick<IntegrationEntry, "slug" | "version">,
): Promise<IntegrationEntry> {
  return apiFetch(`/orgs/${organizationSlug}/integrations/${integration.slug}/archive`, {
    method: "POST",
    headers: { "If-Match": String(integration.version) },
  });
}

export function fetchIntegrationDetail(
  organizationSlug: string,
  integrationSlug: string,
): Promise<IntegrationDetail> {
  return apiFetch(`/orgs/${organizationSlug}/integrations/${integrationSlug}`);
}

export function createMappingDraft(
  organizationSlug: string,
  integrationSlug: string,
  body: { definition: MappingDefinition; target_schema: Record<string, unknown> },
): Promise<MappingVersionRecord> {
  return apiFetch(`/orgs/${organizationSlug}/integrations/${integrationSlug}/mapping-versions`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateMappingDraft(
  organizationSlug: string,
  integrationSlug: string,
  versionId: string,
  recordVersion: number,
  body: { definition: MappingDefinition; target_schema: Record<string, unknown> },
): Promise<MappingVersionRecord> {
  return apiFetch(
    `/orgs/${organizationSlug}/integrations/${integrationSlug}/mapping-versions/${versionId}`,
    {
      method: "PATCH",
      headers: { "If-Match": String(recordVersion) },
      body: JSON.stringify(body),
    },
  );
}

export function validateMappingVersion(
  organizationSlug: string,
  integrationSlug: string,
  versionId: string,
): Promise<MappingValidationResult> {
  return apiFetch(
    `/orgs/${organizationSlug}/integrations/${integrationSlug}` +
      `/mapping-versions/${versionId}/validate`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function publishMappingVersion(
  organizationSlug: string,
  integrationSlug: string,
  versionId: string,
): Promise<MappingVersionRecord> {
  return apiFetch(
    `/orgs/${organizationSlug}/integrations/${integrationSlug}` +
      `/mapping-versions/${versionId}/publish`,
    { method: "POST" },
  );
}

// --- Delivery history (EXP-009) ---

export interface ExportJobEntry {
  id: string;
  document_id: string;
  run_id: string;
  canonical_payload_id: string;
  integration_id: string;
  integration_slug: string | null;
  integration_name: string | null;
  mapping_version_id: string;
  business_key: string;
  state: string;
  attempt_count: number;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ExportAttemptEntry {
  attempt_number: number;
  outcome: string;
  response_status: number | null;
  safe_error: string | null;
  request_sha256: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface ExportDetail {
  job: ExportJobEntry;
  mapping_version_number: number | null;
  attempts: ExportAttemptEntry[];
}

export function fetchExports(
  organizationSlug: string,
  options: { documentId?: string } = {},
): Promise<{ items: ExportJobEntry[] }> {
  const query = options.documentId ? `?document_id=${options.documentId}` : "";
  return apiFetch(`/orgs/${organizationSlug}/exports${query}`);
}

export function fetchExportDetail(
  organizationSlug: string,
  exportJobId: string,
): Promise<ExportDetail> {
  return apiFetch(`/orgs/${organizationSlug}/exports/${exportJobId}`);
}

export function retryExport(
  organizationSlug: string,
  exportJobId: string,
): Promise<ExportJobEntry> {
  return apiFetch(`/orgs/${organizationSlug}/exports/${exportJobId}/retry`, { method: "POST" });
}

export function replayExport(
  organizationSlug: string,
  exportJobId: string,
  reason: string,
): Promise<ExportJobEntry> {
  return apiFetch(`/orgs/${organizationSlug}/exports/${exportJobId}/replay`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// --- Canonical payload (CAN-004) ---

export interface CanonicalPayloadResponse {
  document_id: string;
  run_id: string;
  schema_version: string;
  sha256: string;
  created_at: string;
  created_by: string | null;
  can_copy: boolean;
  redacted: boolean;
  payload: CanonicalOrder;
}

export function fetchCanonicalPayload(
  organizationSlug: string,
  documentId: string,
): Promise<CanonicalPayloadResponse> {
  return apiFetch(`/orgs/${organizationSlug}/documents/${documentId}/canonical-payload`);
}

export function escalateReviewTask(
  organizationSlug: string,
  taskId: string,
  reason: string,
): Promise<ReviewTaskEntry> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/escalate`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

// --- Simulation (AIO-018) ---

export interface SimulationRate {
  current: number;
  candidate: number;
}

export interface SimulationFinding {
  kind: string;
  detail: string;
  waivable: boolean;
}

export interface SimulationFieldDiff {
  field: string;
  current_exact_rate: number | null;
  candidate_exact_rate: number | null;
  delta: number | null;
}

export interface SimulationCohortDiff {
  cohort: string;
  current_exact_rate: number | null;
  candidate_exact_rate: number | null;
  delta: number | null;
}

export interface SimulationDocument {
  document_sha256: string;
  split: string;
  wrong_fields: string[];
  auto_approved: boolean;
}

export interface SimulationComparison {
  current_label: string;
  candidate_label: string;
  field_exact_rate: SimulationRate;
  field_normalized_rate: SimulationRate;
  false_auto_approval_rate: SimulationRate;
  review_rate: SimulationRate;
  total_cost_cents: SimulationRate;
  findings: SimulationFinding[];
  field_diffs: SimulationFieldDiff[];
  cohort_diffs: SimulationCohortDiff[];
  documents: SimulationDocument[];
}

export type SimulationResponse =
  { available: false; reason: string } | { available: true; comparison: SimulationComparison };

export function fetchStreamSimulation(
  organizationSlug: string,
  streamSlug: string,
): Promise<SimulationResponse> {
  return apiFetch(`/orgs/${organizationSlug}/streams/${streamSlug}/simulation`);
}

// --- Provider administration (AIO-019) ---

export interface ProviderEntry {
  name: string;
  capability: string;
  languages: string[];
  region: string;
  local: boolean;
  data_policy: {
    sends_content_to_third_party: boolean;
    retains_content: boolean;
    uses_content_for_training: boolean;
  };
  warnings: string[];
  availability: string;
  description: string;
  health: string;
  approved: boolean;
  credential_configured: boolean;
  credential_id: string | null;
}

export interface ProvidersResponse {
  items: ProviderEntry[];
  health_note: string;
}

export function fetchProviders(organizationSlug: string): Promise<ProvidersResponse> {
  return apiFetch(`/orgs/${organizationSlug}/providers`);
}

export interface RoutingPreviewResult {
  order: string[];
  explanation: string[];
}

export function previewProviderRouting(
  organizationSlug: string,
  body: { capability: string; local_only: boolean; language?: string | null },
): Promise<RoutingPreviewResult> {
  return apiFetch(`/orgs/${organizationSlug}/providers/routing-preview`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface ProviderCredentialEntry {
  id: string;
  provider_name: string;
  label: string;
  kind: "api_key";
  status: "current" | "superseded" | "revoked";
  created_by: string;
  created_at: string;
  superseded_at: string | null;
  revoked_at: string | null;
  revocation_reason: string | null;
}

export function fetchProviderCredentials(
  organizationSlug: string,
): Promise<{ items: ProviderCredentialEntry[] }> {
  return apiFetch(`/orgs/${organizationSlug}/provider-credentials`);
}

export function setProviderCredential(
  organizationSlug: string,
  providerName: string,
  body: { label: string; kind: "api_key"; secret: string },
): Promise<{
  credential: ProviderCredentialEntry;
  rotated: boolean;
  retained_credential_id: string | null;
  detail: string;
}> {
  return apiFetch(`/orgs/${organizationSlug}/providers/${providerName}/credential`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function revokeProviderCredential(
  organizationSlug: string,
  credentialId: string,
  body: { reason: string; force: boolean; confirmation?: string | null },
): Promise<{
  credential: ProviderCredentialEntry;
  revocation_queued: boolean;
  affected_policy_ids: string[];
  detail: string;
}> {
  return apiFetch(`/orgs/${organizationSlug}/provider-credentials/${credentialId}/revoke`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// --- Public-ingestion service credentials (TEN-009) ---

export interface ServiceCredentialEntry {
  id: string;
  name: string;
  key_prefix: string;
  scopes: Array<"documents.upload">;
  allowed_stream_ids: string[];
  status: "active" | "revoked";
  expires_at: string | null;
  last_used_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  version: number;
}

export interface ServiceCredentialSecretResponse {
  credential: ServiceCredentialEntry;
  api_key: string;
  warning: string;
}

export function fetchServiceCredentials(
  organizationSlug: string,
): Promise<{ items: ServiceCredentialEntry[] }> {
  return apiFetch(`/orgs/${organizationSlug}/service-credentials`);
}

export function createServiceCredential(
  organizationSlug: string,
  body: {
    name: string;
    scopes: Array<"documents.upload">;
    allowed_stream_ids: string[];
    expires_in_days: number;
  },
): Promise<ServiceCredentialSecretResponse> {
  return apiFetch(`/orgs/${organizationSlug}/service-credentials`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function rotateServiceCredential(
  organizationSlug: string,
  credential: Pick<ServiceCredentialEntry, "id" | "version">,
  expiresInDays = 90,
): Promise<ServiceCredentialSecretResponse> {
  return apiFetch(`/orgs/${organizationSlug}/service-credentials/${credential.id}/rotate`, {
    method: "POST",
    headers: { "If-Match": String(credential.version) },
    body: JSON.stringify({ expires_in_days: expiresInDays }),
  });
}

export function revokeServiceCredential(
  organizationSlug: string,
  credential: Pick<ServiceCredentialEntry, "id" | "version">,
): Promise<{ credential: ServiceCredentialEntry }> {
  return apiFetch(`/orgs/${organizationSlug}/service-credentials/${credential.id}/revoke`, {
    method: "POST",
    headers: { "If-Match": String(credential.version) },
  });
}

export type AdminPolicyType = "provider" | "confidence";

export interface AdminPolicyVersion {
  id: string;
  policy_type: AdminPolicyType;
  version_number: number;
  state: "draft" | "published" | "superseded";
  definition: Record<string, unknown>;
  change_summary: string | null;
  published_at: string | null;
  published_by: string | null;
  version: number;
}

export interface PolicyValidationFinding {
  level: "error" | "warning";
  path: string;
  message: string;
}

export function fetchAdminPolicies(
  organizationSlug: string,
  policyType: AdminPolicyType,
): Promise<{ items: AdminPolicyVersion[] }> {
  return apiFetch(`/orgs/${organizationSlug}/policies/${policyType}`);
}

export function createAdminPolicyDraft(
  organizationSlug: string,
  policyType: AdminPolicyType,
  body: { definition: Record<string, unknown>; change_summary: string | null },
): Promise<AdminPolicyVersion> {
  return apiFetch(`/orgs/${organizationSlug}/policies/${policyType}/drafts`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function validateAdminPolicy(
  organizationSlug: string,
  policyType: AdminPolicyType,
  policyId: string,
): Promise<{ valid: boolean; findings: PolicyValidationFinding[] }> {
  return apiFetch(`/orgs/${organizationSlug}/policies/${policyType}/${policyId}/validate`, {
    method: "POST",
  });
}

export function publishAdminPolicy(
  organizationSlug: string,
  policyType: AdminPolicyType,
  policyId: string,
): Promise<AdminPolicyVersion> {
  return apiFetch(`/orgs/${organizationSlug}/policies/${policyType}/${policyId}/publish`, {
    method: "POST",
  });
}

// --- Catalogs (CAT-004/005) ---

export interface CatalogSummary {
  id: string;
  name: string;
  slug: string;
  catalog_type: string;
  source: string;
  active_version_id: string | null;
}

export interface CatalogVersionSummary {
  id: string;
  version_number: number;
  state: string;
  record_count: number;
  change_summary: string | null;
  published_at: string | null;
  published_by: string | null;
}

export interface CatalogImportMapping {
  source_id: string;
  display_name: string;
  aliases?: string | null;
  effective_from?: string | null;
  effective_to?: string | null;
  attributes?: Record<string, string>;
}

export interface CatalogImportResult {
  status: "previewed" | "draft_created" | "parsed";
  records: number;
  issues: { row_number: number; message: string }[];
  warnings: string[];
  encoding: string;
  preview: {
    added: string[];
    changed: string[];
    deactivated: string[];
    unchanged: number;
  };
  version: CatalogVersionSummary | null;
}

export interface CatalogRecordEntry {
  id: string;
  source_id: string;
  display_name: string;
  aliases: string[];
  attributes: Record<string, string>;
  effective_from: string | null;
  effective_to: string | null;
}

export function fetchCatalogs(organizationSlug: string): Promise<{ items: CatalogSummary[] }> {
  return apiFetch(`/orgs/${organizationSlug}/catalogs`);
}

export function fetchCatalogDetail(
  organizationSlug: string,
  catalogSlug: string,
): Promise<{ catalog: CatalogSummary; versions: CatalogVersionSummary[] }> {
  return apiFetch(`/orgs/${organizationSlug}/catalogs/${catalogSlug}`);
}

export function importCatalogFile(
  organizationSlug: string,
  catalogSlug: string,
  body: {
    filename: string;
    content_base64: string;
    mapping: CatalogImportMapping;
    sheet?: string | null;
    allow_partial?: boolean;
    dry_run?: boolean;
  },
): Promise<CatalogImportResult> {
  return apiFetch(`/orgs/${organizationSlug}/catalogs/${catalogSlug}/imports`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function fetchCatalogRecords(
  organizationSlug: string,
  catalogSlug: string,
  versionId: string,
  options: { q?: string; cursor?: string } = {},
): Promise<{ items: CatalogRecordEntry[]; has_more: boolean; next_cursor: string | null }> {
  const params = new URLSearchParams();
  if (options.q) params.set("q", options.q);
  if (options.cursor) params.set("cursor", options.cursor);
  const suffix = params.size > 0 ? `?${params.toString()}` : "";
  return apiFetch(
    `/orgs/${organizationSlug}/catalogs/${catalogSlug}/versions/${versionId}/records${suffix}`,
  );
}

export function activateCatalogVersion(
  organizationSlug: string,
  catalogSlug: string,
  versionId: string,
): Promise<CatalogVersionSummary> {
  return apiFetch(
    `/orgs/${organizationSlug}/catalogs/${catalogSlug}/versions/${versionId}/activate`,
    { method: "POST" },
  );
}

// --- Extraction training: stream-scoped training sets + annotation (Phase 1) ---

export interface TrainingRegion {
  page_number: number;
  polygon: number[][];
}

export interface TrainingGroundTruth {
  fields: Record<string, string | null>;
  lines?: Record<string, string | null>[];
  validations?: string[];
  regions?: Record<string, TrainingRegion>;
}

export interface TrainingSetSummary {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  stream_id: string | null;
  privacy_classification: string;
  working_draft_version_id: string | null;
  published_version_id: string | null;
  document_count: number;
}

export interface TrainingVersionSummary {
  id: string;
  version_number: number;
  state: "draft" | "published" | "superseded";
  published_at: string | null;
  counts: Record<string, number>;
}

export interface TrainingGoldDocument {
  id: string;
  dataset_version_id: string;
  source_document_id: string | null;
  document_sha256: string;
  split: "train" | "validation" | "test";
  expected_class: string | null;
  ground_truth: TrainingGroundTruth;
}

export interface TrainingSetDetail extends TrainingSetSummary {
  versions: TrainingVersionSummary[];
  documents: TrainingGoldDocument[];
}

function trainingBase(organizationSlug: string, streamSlug: string): string {
  return `/orgs/${organizationSlug}/streams/${streamSlug}/training-sets`;
}

export function fetchTrainingSets(
  organizationSlug: string,
  streamSlug: string,
): Promise<{ items: TrainingSetSummary[] }> {
  return apiFetch(trainingBase(organizationSlug, streamSlug));
}

export function createTrainingSet(
  organizationSlug: string,
  streamSlug: string,
  body: { name: string; slug: string; description?: string | null },
): Promise<TrainingSetSummary> {
  return apiFetch(trainingBase(organizationSlug, streamSlug), {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function fetchTrainingSet(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
): Promise<TrainingSetDetail> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}`);
}

export function updateTrainingSet(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
  body: { name?: string; description?: string | null },
): Promise<TrainingSetSummary> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteTrainingSet(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
): Promise<void> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}`, {
    method: "DELETE",
  });
}

export function startTrainingDraft(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
): Promise<TrainingVersionSummary> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}/versions`, {
    method: "POST",
  });
}

export function publishTrainingSet(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
): Promise<TrainingVersionSummary> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}/publish`, {
    method: "POST",
  });
}

export function fetchTrainingDocuments(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
): Promise<{ items: TrainingGoldDocument[] }> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}/documents`);
}

export function upsertTrainingDocument(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
  body: {
    source_document_id: string;
    split: "train" | "validation" | "test";
    expected_class?: string | null;
    ground_truth: TrainingGroundTruth;
  },
): Promise<TrainingGoldDocument> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}/documents`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function deleteTrainingDocument(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
  goldDocumentId: string,
): Promise<void> {
  return apiFetch(
    `${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}/documents/${goldDocumentId}`,
    { method: "DELETE" },
  );
}

export interface TextInRegionResult {
  text: string;
  reason?: string;
}

export function documentTextInRegion(
  organizationSlug: string,
  documentId: string,
  body: { page_number: number; polygon: number[][] },
): Promise<TextInRegionResult> {
  return apiFetch(`/orgs/${organizationSlug}/documents/${documentId}/text-in-region`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// --- Extraction training: compile few-shot + evaluate the lift (Phases 2-3) ---

export interface CompiledExamples {
  instruction_version_id: string;
  stream_version_id: string;
  version_number: number;
  state: string;
  reference: string;
  example_count: number;
}

export function compileTrainingExamples(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
): Promise<CompiledExamples> {
  return apiFetch(
    `${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}/compile-examples`,
    { method: "POST" },
  );
}

export interface TrainingEvaluation {
  id: string;
  dataset_version_id: string;
  stream_version_id: string | null;
  baseline_run_id: string | null;
  execution_mode: string;
  state: string;
  promotion_eligible: boolean;
  by_field: Record<string, { total: number; exact: number; normalized: number }>;
  by_cohort: Record<string, Record<string, number>>;
  field_diffs: {
    field: string;
    current_exact_rate: number;
    candidate_exact_rate: number;
    delta: number;
  }[];
  findings: unknown[];
  safe_error: string | null;
  created_at: string;
  finished_at: string | null;
}

export function evaluateTrainingSet(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
  body: {
    execution_mode?: "server" | "simulation";
    stream_version_id?: string;
    baseline_run_id?: string;
    predictions?: Record<string, Record<string, unknown>>;
  } = {},
): Promise<TrainingEvaluation> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}/evaluate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function fetchTrainingEvaluations(
  organizationSlug: string,
  streamSlug: string,
  trainingSlug: string,
): Promise<{ items: TrainingEvaluation[] }> {
  return apiFetch(`${trainingBase(organizationSlug, streamSlug)}/${trainingSlug}/evaluations`);
}

// --- Skills home (skill = stream; grouped by process until intakes land) ---

export interface SkillSummary {
  id: string;
  slug: string;
  name: string;
  status: string;
  process_slug: string | null;
  process_name: string | null;
  in_review: number;
  received_30d: number;
  field_accuracy: number | null;
  trained_version: number | null;
}

export function fetchSkillsOverview(organizationSlug: string): Promise<{ items: SkillSummary[] }> {
  return apiFetch(`/orgs/${organizationSlug}/skills`);
}

// --- Routing: unrouted queue + manual route (classifier backend) ---

export interface UnroutedDocument {
  id: string;
  stream_id: string;
  original_filename: string;
  received_at: string;
  state_reason: string | null;
}

export function fetchUnroutedDocuments(
  organizationSlug: string,
): Promise<{ items: UnroutedDocument[] }> {
  return apiFetch(`/orgs/${organizationSlug}/routing/unrouted`);
}

export function routeDocument(
  organizationSlug: string,
  documentId: string,
  streamSlug: string,
): Promise<{ document_id: string; stream_slug: string; state: string }> {
  return apiFetch(`/orgs/${organizationSlug}/documents/${documentId}/route`, {
    method: "POST",
    body: JSON.stringify({ stream_slug: streamSlug }),
  });
}
