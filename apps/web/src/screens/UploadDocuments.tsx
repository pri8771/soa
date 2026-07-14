/**
 * Upload documents (ING-008, UI_UX_BLUEPRINT §5.2).
 *
 * Files are picked per stream, validated client-side (type/size) before
 * any network call, then processed independently: declare → PUT to the
 * signed target → complete. Every file shows its own status and OUTCOME
 * — queued, duplicate (flagged or refused), rejected, quarantined,
 * scan-pending, failed — because "some of your files didn't make it" is
 * exactly the moment a user needs specifics, not a spinner. Files can
 * be removed before upload and cancelled mid-flight (the session is
 * aborted server-side). Keyboard-first: the drop zone is also a plain
 * file-picker button, and per-file actions are labeled buttons.
 */

import { Badge, Banner, Button, Select } from "@soa/design-system";
import { useQuery } from "@tanstack/react-query";
import { useRef, useState } from "react";

import {
  abortUploadSession,
  ApiError,
  completeUploadSession,
  createUploadSession,
  fetchStreams,
  readBlobBytes,
  sha256OfFile,
  type UploadCompleteResult,
} from "../api/client";
import { AppShell } from "../shell/AppShell";
import { HelpTip } from "../components/help/HelpTip";
import { useShellSession } from "../shell/ShellContext";

const ACCEPTED_TYPES: Record<string, string> = {
  "application/pdf": "PDF",
  "image/png": "PNG",
  "image/jpeg": "JPEG",
  "image/tiff": "TIFF",
};
const MAX_SIZE_BYTES = 52_428_800; // platform maximum; streams may be stricter

type FileStatus =
  | { phase: "ready" }
  | { phase: "invalid"; message: string }
  | { phase: "uploading"; sessionId?: string }
  | { phase: "cancelled" }
  | { phase: "failed"; message: string }
  | { phase: "done"; result: UploadCompleteResult };

interface QueuedFile {
  id: number;
  file: File;
  status: FileStatus;
}

function validate(file: File): FileStatus {
  if (!(file.type in ACCEPTED_TYPES)) {
    const supported = Object.values(ACCEPTED_TYPES).join(", ");
    return { phase: "invalid", message: `Unsupported type — accepted: ${supported}` };
  }
  if (file.size > MAX_SIZE_BYTES) {
    return { phase: "invalid", message: "Larger than the 50 MB limit" };
  }
  if (file.size === 0) {
    return { phase: "invalid", message: "Empty file" };
  }
  return { phase: "ready" };
}

function OutcomeBadge({ status }: { status: FileStatus }) {
  switch (status.phase) {
    case "ready":
      return <Badge tone="neutral">Ready</Badge>;
    case "invalid":
      return <Badge tone="critical">Not uploadable</Badge>;
    case "uploading":
      return <Badge tone="accent">Uploading…</Badge>;
    case "cancelled":
      return <Badge tone="neutral">Cancelled</Badge>;
    case "failed":
      return <Badge tone="critical">Failed</Badge>;
    case "done":
      break;
  }
  const { result } = status;
  if (result.state === "rejected") return <Badge tone="critical">Rejected</Badge>;
  if (result.state === "quarantined") return <Badge tone="critical">Quarantined</Badge>;
  if (result.state === "failed_retryable") return <Badge tone="warning">Scan pending</Badge>;
  if (result.duplicate_of) return <Badge tone="warning">Duplicate</Badge>;
  return <Badge tone="success">Queued</Badge>;
}

function outcomeDetail(status: FileStatus): string | null {
  if (status.phase === "invalid" || status.phase === "failed") return status.message;
  if (status.phase !== "done") return null;
  const { result } = status;
  if (result.state_reason) return result.state_reason;
  if (result.duplicate_of) {
    return "Identical content was already uploaded; this copy is flagged for review.";
  }
  if (result.state === "queued") return "Queued for processing.";
  return null;
}

export function UploadDocuments() {
  const session = useShellSession();
  const slug = session.organization.slug;
  const canUpload = session.permissions.has("documents.upload");
  const inputRef = useRef<HTMLInputElement>(null);
  const nextId = useRef(1);
  const cancelledIds = useRef(new Set<number>());

  const streams = useQuery({ queryKey: ["streams", slug], queryFn: () => fetchStreams(slug) });
  const activeStreams = (streams.data ?? []).filter((s) => s.status !== "archived");
  const [streamSlug, setStreamSlug] = useState<string | null>(null);
  const selectedStream = streamSlug ?? activeStreams[0]?.slug ?? null;

  const [files, setFiles] = useState<QueuedFile[]>([]);
  const [dragging, setDragging] = useState(false);

  const patchFile = (id: number, status: FileStatus) =>
    setFiles((current) => current.map((f) => (f.id === id ? { ...f, status } : f)));

  const addFiles = (incoming: FileList | File[]) => {
    const additions = [...incoming].map((file) => ({
      id: nextId.current++,
      file,
      status: validate(file),
    }));
    setFiles((current) => [...current, ...additions]);
  };

  const uploadOne = async (queued: QueuedFile, targetStream: string) => {
    patchFile(queued.id, { phase: "uploading" });
    try {
      const bytes = await readBlobBytes(queued.file);
      const sha256 = await sha256OfFile(new Blob([bytes]));
      const created = await createUploadSession(slug, targetStream, {
        filename: queued.file.name,
        content_type: queued.file.type,
        size_bytes: queued.file.size,
        sha256,
      });
      if (cancelledIds.current.has(queued.id)) {
        void abortUploadSession(slug, created.session_id).catch(() => undefined);
        return;
      }
      patchFile(queued.id, { phase: "uploading", sessionId: created.session_id });
      const put = await fetch(created.upload_url, {
        method: created.upload_method,
        body: bytes,
        headers: { "Content-Type": queued.file.type },
      });
      if (!put.ok) {
        throw new ApiError(put.status, `Storage refused the upload (${put.status}).`);
      }
      if (cancelledIds.current.has(queued.id)) return;
      const result = await completeUploadSession(slug, created.session_id);
      if (cancelledIds.current.has(queued.id)) return;
      patchFile(queued.id, { phase: "done", result });
    } catch (error) {
      // A cancelled file was already marked; don't overwrite it.
      setFiles((current) =>
        current.map((f) =>
          f.id === queued.id && f.status.phase === "uploading"
            ? {
                ...f,
                status: {
                  phase: "failed",
                  message: error instanceof Error ? error.message : "Upload failed.",
                },
              }
            : f,
        ),
      );
    }
  };

  const uploadAll = () => {
    if (!selectedStream) return;
    for (const queued of files) {
      if (queued.status.phase === "ready") {
        void uploadOne(queued, selectedStream);
      }
    }
  };

  const cancelFile = (queued: QueuedFile) => {
    cancelledIds.current.add(queued.id);
    if (queued.status.phase === "uploading" && queued.status.sessionId) {
      void abortUploadSession(slug, queued.status.sessionId).catch(() => undefined);
    }
    patchFile(queued.id, { phase: "cancelled" });
  };

  const readyCount = files.filter((f) => f.status.phase === "ready").length;
  const doneCount = files.filter((f) => f.status.phase === "done").length;
  const problemCount = files.filter((f) =>
    f.status.phase === "done"
      ? f.status.result.state === "rejected" || f.status.result.state === "quarantined"
      : f.status.phase === "failed",
  ).length;

  return (
    <AppShell
      title="Upload documents"
      breadcrumbs={[
        { label: session.organization.name },
        { label: "Documents" },
        { label: "Upload" },
      ]}
      actions={<HelpTip topic="upload" />}
    >
      <div style={{ display: "grid", gap: "var(--soa-space-5)", maxWidth: "56rem" }}>
        {!canUpload ? (
          <Banner tone="warning" title="You can’t upload documents">
            Your role does not include the documents.upload permission.
          </Banner>
        ) : null}
        {streams.status === "error" ? (
          <Banner tone="critical" title="Couldn’t load streams">
            Nothing has been changed.
          </Banner>
        ) : null}

        <div style={{ maxWidth: "20rem" }}>
          <Select
            label="Stream"
            items={activeStreams.map((s) => ({ id: s.slug, label: s.name }))}
            selectedKey={selectedStream}
            onSelectionChange={(key) => setStreamSlug(String(key))}
          />
        </div>

        <div
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            if (canUpload) addFiles(event.dataTransfer.files);
          }}
          style={{
            border: `2px dashed var(${dragging ? "--soa-accent" : "--soa-border"})`,
            borderRadius: "var(--soa-radius-panel)",
            padding: "var(--soa-space-6)",
            textAlign: "center",
            display: "grid",
            gap: "var(--soa-space-3)",
            justifyItems: "center",
          }}
        >
          <p style={{ margin: 0 }}>Drag files here, or</p>
          <Button onPress={() => inputRef.current?.click()} isDisabled={!canUpload}>
            Choose files
          </Button>
          <input
            ref={inputRef}
            type="file"
            multiple
            accept={Object.keys(ACCEPTED_TYPES).join(",")}
            aria-label="Choose files to upload"
            style={{ display: "none" }}
            onChange={(event) => {
              if (event.target.files) addFiles(event.target.files);
              event.target.value = "";
            }}
          />
          <p style={{ margin: 0, font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}>
            PDF, PNG, JPEG, or TIFF · up to 50 MB per file
          </p>
        </div>

        {files.length > 0 ? (
          <>
            <div style={{ display: "flex", gap: "var(--soa-space-3)", alignItems: "center" }}>
              <Button
                onPress={uploadAll}
                isDisabled={!canUpload || readyCount === 0 || !selectedStream}
              >
                Upload {readyCount > 0 ? `${readyCount} file${readyCount === 1 ? "" : "s"}` : ""}
              </Button>
              <span aria-live="polite" style={{ font: "var(--soa-font-caption)" }}>
                {doneCount > 0 || problemCount > 0
                  ? `${doneCount} processed · ${problemCount} need attention`
                  : null}
              </span>
            </div>

            <ul
              style={{
                listStyle: "none",
                margin: 0,
                padding: 0,
                display: "grid",
                gap: "var(--soa-space-2)",
              }}
            >
              {files.map((queued) => (
                <li
                  key={queued.id}
                  style={{
                    display: "flex",
                    gap: "var(--soa-space-3)",
                    alignItems: "center",
                    flexWrap: "wrap",
                    padding: "var(--soa-space-3)",
                    border: "1px solid var(--soa-border)",
                    borderRadius: "var(--soa-radius-control)",
                  }}
                >
                  <span style={{ fontWeight: 600 }}>{queued.file.name}</span>
                  <OutcomeBadge status={queued.status} />
                  {outcomeDetail(queued.status) ? (
                    <span
                      style={{ font: "var(--soa-font-caption)", color: "var(--soa-text-muted)" }}
                    >
                      {outcomeDetail(queued.status)}
                    </span>
                  ) : null}
                  {queued.status.phase === "ready" ? (
                    <Button
                      size="sm"
                      variant="subtle"
                      aria-label={`Remove ${queued.file.name}`}
                      onPress={() =>
                        setFiles((current) => current.filter((f) => f.id !== queued.id))
                      }
                    >
                      Remove
                    </Button>
                  ) : null}
                  {queued.status.phase === "uploading" ? (
                    <Button
                      size="sm"
                      variant="subtle"
                      aria-label={`Cancel ${queued.file.name}`}
                      onPress={() => cancelFile(queued)}
                    >
                      Cancel
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </div>
    </AppShell>
  );
}
