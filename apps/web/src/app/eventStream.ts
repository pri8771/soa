/**
 * SSE push channel client (JOB-007-adjacent live-update UX).
 *
 * The API polls the transactional outbox server-side and pushes a small
 * "something changed" notice per event — never the payload. Browser
 * `EventSource` cannot send auth headers, so this connects with a plain
 * `fetch()` and reads the response body as a stream instead.
 *
 * If the stream is down, this does nothing special: existing UI polling
 * (`liveQuery.ts`) remains the fallback, and this hook just keeps
 * reconnecting with exponential backoff in the background.
 */

import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { requestHeaders } from "../api/client";
import { getBearerToken } from "../auth/runtime";
import { env } from "../env";

export interface SseFrame {
  id?: string;
  event?: string;
  data: string;
}

/** Query key prefixes to invalidate on every received event — one entry
 * per queue/dashboard screen that live-updates (JOB-007). Matches by
 * prefix, so e.g. ["documents", slug] also invalidates
 * ["documents", slug, stateFilter, channelFilter, searchText]. */
const INVALIDATED_QUERY_KEY_PREFIXES: readonly string[] = [
  "documents",
  "review-tasks",
  "jobs",
  "jobs-stats",
];

const RECONNECT_INITIAL_DELAY_MS = 1_000;
const RECONNECT_MAX_DELAY_MS = 30_000;

/** Incrementally parses SSE frames out of a chunked text stream. Frames
 * are separated by a blank line; each line is a `field: value` pair (or a
 * `:`-prefixed comment/keepalive, which carries no field and is ignored). */
export class SseFrameParser {
  private buffer = "";

  /** Feed one decoded text chunk; returns every complete frame it made
   * available (zero, one, or many — a chunk may contain several frames,
   * and a frame may be split across chunks). */
  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const raw = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const frame = parseFrame(raw);
      if (frame) frames.push(frame);
      boundary = this.buffer.indexOf("\n\n");
    }
    return frames;
  }
}

function parseFrame(raw: string): SseFrame | null {
  const fields: { id?: string; event?: string; data?: string } = {};
  for (const line of raw.split("\n")) {
    if (line === "" || line.startsWith(":")) continue; // comment/keepalive
    const separator = line.indexOf(":");
    if (separator === -1) continue;
    const field = line.slice(0, separator);
    const value = line.slice(separator + 1).replace(/^ /, "");
    if (field === "id") fields.id = value;
    else if (field === "event") fields.event = value;
    else if (field === "data") fields.data = fields.data === undefined ? value : `${fields.data}\n${value}`;
  }
  if (fields.data === undefined) return null; // comment-only frame
  return { id: fields.id, event: fields.event, data: fields.data };
}

async function invalidateForEvent(queryClient: QueryClient, organizationSlug: string): Promise<void> {
  await Promise.all(
    INVALIDATED_QUERY_KEY_PREFIXES.map((prefix) =>
      queryClient.invalidateQueries({ queryKey: [prefix, organizationSlug] }),
    ),
  );
}

/** One connect-read-reconnect cycle. Exported for the frame-parser/hook
 * tests to drive with a mocked `fetch`; production code only uses the
 * `useEventStream` hook below. */
export async function runEventStream(
  organizationSlug: string,
  queryClient: QueryClient,
  signal: AbortSignal,
  options: {
    fetchImpl?: typeof fetch;
    onReconnectDelay?: (ms: number) => void;
  } = {},
): Promise<void> {
  const fetchImpl = options.fetchImpl ?? fetch;
  let lastEventId: string | null = null;
  let delay = RECONNECT_INITIAL_DELAY_MS;

  while (!signal.aborted) {
    try {
      const params = new URLSearchParams();
      if (lastEventId) params.set("after_id", lastEventId);
      const query = params.size > 0 ? `?${params.toString()}` : "";
      const url = `${env.VITE_API_BASE_URL}/orgs/${organizationSlug}/events/stream${query}`;
      const bearerToken = await getBearerToken();
      const response = await fetchImpl(url, {
        headers: requestHeaders(undefined, bearerToken),
        signal,
      });
      if (!response.ok || !response.body) {
        throw new Error(`event stream request failed (${response.status})`);
      }
      delay = RECONNECT_INITIAL_DELAY_MS; // a successful connect resets backoff
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parser = new SseFrameParser();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const frames = parser.push(decoder.decode(value, { stream: true }));
        for (const frame of frames) {
          if (frame.id) lastEventId = frame.id;
          await invalidateForEvent(queryClient, organizationSlug);
        }
      }
    } catch {
      if (signal.aborted) return;
      // Stream down: no special UI — existing polling is the fallback.
      // Reconnect with exponential backoff (1s -> 30s cap).
    }
    if (signal.aborted) return;
    options.onReconnectDelay?.(delay);
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, RECONNECT_MAX_DELAY_MS);
  }
}

/** Mount ONCE in the authenticated app shell (not per-screen): connects
 * on mount, reconnects on error/close, and invalidates the live-query
 * screens' query keys as events arrive. */
export function useEventStream(organizationSlug: string): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    const controller = new AbortController();
    void runEventStream(organizationSlug, queryClient, controller.signal);
    return () => controller.abort();
  }, [organizationSlug, queryClient]);
}
