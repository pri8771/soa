/**
 * Delivery history (EXP-009): every export job for a document with its
 * append-only attempts — status, SAFE error, request hash, and the
 * pinned mapping version. No headers were ever stored, so no secret
 * header can appear here.
 *
 * Controls are permission-separated: anyone who can read integrations
 * sees the history; RETRY (retryable failures) and REPLAY (settled
 * failures, reason required) need integrations.replay, and the disabled
 * state says so.
 */

import { Badge, Banner, Button, Skeleton, TextField } from "@soa/design-system";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  fetchExportDetail,
  fetchExports,
  replayExport,
  retryExport,
  type ExportJobEntry,
} from "../../api/client";
import { QUEUE_POLL_MS } from "../../app/liveQuery";

const STATE_TONES: Record<string, "neutral" | "success" | "warning" | "critical" | "info"> = {
  pending: "info",
  in_progress: "info",
  succeeded: "success",
  failed_retryable: "warning",
  failed_terminal: "critical",
  cancelled: "neutral",
};

//: Settled export-job states (the worker's export orchestrator treats
//: these as no-ops); anything else may still change, so keep polling.
const TERMINAL_STATES = new Set(["succeeded", "failed_terminal", "cancelled"]);

function ExportJobRow({
  organizationSlug,
  job,
  canReplay,
}: {
  organizationSlug: string;
  job: ExportJobEntry;
  canReplay: boolean;
}) {
  const queryClient = useQueryClient();
  const [replayReason, setReplayReason] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["export-detail", organizationSlug, job.id],
    queryFn: () => fetchExportDetail(organizationSlug, job.id),
    // Attempts keep appending while the job is unsettled.
    refetchInterval: TERMINAL_STATES.has(job.state) ? false : QUEUE_POLL_MS,
  });
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["exports"] });
    void queryClient.invalidateQueries({ queryKey: ["export-detail", organizationSlug, job.id] });
  };
  const retry = useMutation({
    mutationFn: () => retryExport(organizationSlug, job.id),
    onSuccess: () => {
      setMessage("Retry queued — same payload, same business key.");
      refresh();
    },
    onError: (error: unknown) =>
      setMessage(error instanceof Error ? `Retry failed: ${error.message}` : "Retry failed."),
  });
  const replay = useMutation({
    mutationFn: (reason: string) => replayExport(organizationSlug, job.id, reason),
    onSuccess: () => {
      setMessage("Replay queued with the audited reason.");
      setReplayReason("");
      refresh();
    },
    onError: (error: unknown) =>
      setMessage(error instanceof Error ? `Replay failed: ${error.message}` : "Replay failed."),
  });

  return (
    <li
      aria-label={`Export via ${job.integration_name ?? job.integration_slug ?? "integration"}`}
      style={{
        border: "1px solid var(--soa-border)",
        borderRadius: "var(--soa-radius-md)",
        padding: "var(--soa-space-3)",
        display: "grid",
        gap: "var(--soa-space-2)",
      }}
    >
      <div
        style={{
          display: "flex",
          gap: "var(--soa-space-2)",
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <strong>{job.integration_name ?? job.integration_slug ?? "integration"}</strong>
        <Badge tone={STATE_TONES[job.state] ?? "neutral"}>{job.state.replace(/_/g, " ")}</Badge>
        {detail.data?.mapping_version_number != null ? (
          <Badge tone="neutral">{`mapping v${detail.data.mapping_version_number}`}</Badge>
        ) : null}
        <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
          {job.business_key}
        </span>
      </div>
      {job.last_error ? (
        <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>{job.last_error}</p>
      ) : null}

      {detail.status === "pending" ? <Skeleton height="2rem" /> : null}
      {detail.data && detail.data.attempts.length > 0 ? (
        <ol style={{ margin: 0, paddingInlineStart: "1.2rem" }}>
          {detail.data.attempts.map((attempt) => (
            <li key={attempt.attempt_number} style={{ font: "var(--soa-font-body-sm)" }}>
              attempt {attempt.attempt_number}: {attempt.outcome.replace(/_/g, " ")}
              {attempt.response_status !== null ? ` (HTTP ${attempt.response_status})` : ""}
              {attempt.safe_error ? ` — ${attempt.safe_error}` : ""}
              {attempt.request_sha256 ? (
                <span style={{ color: "var(--soa-text-muted)" }}>
                  {` · payload sha ${attempt.request_sha256.slice(0, 12)}…`}
                </span>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}
      {detail.data && detail.data.attempts.length === 0 ? (
        <p style={{ margin: 0, color: "var(--soa-text-muted)" }}>No delivery attempts yet.</p>
      ) : null}

      <div role="status" aria-live="polite">
        {message ? <p style={{ margin: 0 }}>{message}</p> : null}
      </div>

      {job.state === "failed_retryable" ? (
        <div style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "center" }}>
          <Button size="sm" isDisabled={!canReplay} onPress={() => retry.mutate()}>
            Retry delivery
          </Button>
          {!canReplay ? (
            <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
              Retrying needs the integrations.replay permission.
            </span>
          ) : null}
        </div>
      ) : null}
      {job.state === "failed_terminal" || job.state === "cancelled" ? (
        <div style={{ display: "grid", gap: "var(--soa-space-2)" }}>
          <TextField
            label="Replay reason (required; recorded in the audit log)"
            value={replayReason}
            onChange={setReplayReason}
            isReadOnly={!canReplay}
          />
          <div style={{ display: "flex", gap: "var(--soa-space-2)", alignItems: "center" }}>
            <Button
              size="sm"
              isDisabled={!canReplay || replayReason.trim() === ""}
              onPress={() => replay.mutate(replayReason.trim())}
            >
              Replay delivery
            </Button>
            {!canReplay ? (
              <span style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
                Replaying needs the integrations.replay permission.
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
    </li>
  );
}

export function DeliveryHistory({
  organizationSlug,
  documentId,
  canReplay,
}: {
  organizationSlug: string;
  documentId: string;
  canReplay: boolean;
}) {
  const exports = useQuery({
    queryKey: ["exports", organizationSlug, documentId],
    queryFn: () => fetchExports(organizationSlug, { documentId }),
    // Poll only while a job is still moving; a settled history is static.
    refetchInterval: (query) =>
      query.state.data?.items.some((job) => !TERMINAL_STATES.has(job.state))
        ? QUEUE_POLL_MS
        : false,
  });

  if (exports.status === "pending") return <Skeleton height="4rem" />;
  if (exports.status === "error") {
    return (
      <Banner tone="critical" title="Couldn’t load deliveries">
        The service did not respond.
      </Banner>
    );
  }
  if (exports.data.items.length === 0) {
    return (
      <Badge tone="neutral">
        no deliveries yet — exports are scheduled when a document is approved
      </Badge>
    );
  }
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "0.75rem" }}>
      {exports.data.items.map((job) => (
        <ExportJobRow
          key={job.id}
          organizationSlug={organizationSlug}
          job={job}
          canReplay={canReplay}
        />
      ))}
    </ul>
  );
}
