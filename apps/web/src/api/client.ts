/**
 * API client (TEN-012): thin fetch wrapper over the control-plane API.
 *
 * Development identity: when a dev user is selected (stored locally), the
 * X-Dev-User header rides on every request — the server only honors it in
 * development/test environments (TEN-002).
 */

import { env } from "../env";
import type { CanonicalOrder } from "./canonical-order";

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
}

export function fetchDocumentDetail(
  organizationSlug: string,
  documentId: string,
): Promise<DocumentDetail> {
  return apiFetch<DocumentDetail>(`/orgs/${organizationSlug}/documents/${documentId}`);
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

export function correctField(
  organizationSlug: string,
  taskId: string,
  body: {
    field_key: string;
    row_index?: number | null;
    value: string | null;
    reason?: string;
    expected_version: number;
  },
): Promise<CorrectionResult> {
  return apiFetch(`/orgs/${organizationSlug}/review-tasks/${taskId}/corrections`, {
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
  status: string;
  endpoint_url: string | null;
  credential_configured: boolean;
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
