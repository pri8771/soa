import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { runEventStream, SseFrameParser } from "./eventStream";

describe("SseFrameParser", () => {
  it("parses a single complete frame", () => {
    const parser = new SseFrameParser();
    const frames = parser.push('id: 1\nevent: document.approved\ndata: {"a":1}\n\n');
    expect(frames).toEqual([{ id: "1", event: "document.approved", data: '{"a":1}' }]);
  });

  it("parses multiple frames delivered in one chunk", () => {
    const parser = new SseFrameParser();
    const frames = parser.push(
      'id: 1\nevent: a\ndata: {"x":1}\n\nid: 2\nevent: b\ndata: {"x":2}\n\n',
    );
    expect(frames).toHaveLength(2);
    expect(frames[0]).toEqual({ id: "1", event: "a", data: '{"x":1}' });
    expect(frames[1]).toEqual({ id: "2", event: "b", data: '{"x":2}' });
  });

  it("reassembles a frame split across chunks", () => {
    const parser = new SseFrameParser();
    expect(parser.push("id: 1\nev")).toEqual([]);
    expect(parser.push('ent: document.approved\ndata: {"a"')).toEqual([]);
    const frames = parser.push(':1}\n\n');
    expect(frames).toEqual([{ id: "1", event: "document.approved", data: '{"a":1}' }]);
  });

  it("splits a chunk mid-boundary across two pushes", () => {
    const parser = new SseFrameParser();
    expect(parser.push("id: 1\nevent: a\ndata: x\n")).toEqual([]);
    const frames = parser.push("\n");
    expect(frames).toEqual([{ id: "1", event: "a", data: "x" }]);
  });

  it("ignores comment/keepalive-only frames", () => {
    const parser = new SseFrameParser();
    const frames = parser.push(": keepalive\n\n");
    expect(frames).toEqual([]);
  });

  it("ignores interleaved comments between real frames", () => {
    const parser = new SseFrameParser();
    const frames = parser.push('id: 1\nevent: a\ndata: x\n\n: keepalive\n\nid: 2\nevent: b\ndata: y\n\n');
    expect(frames).toEqual([
      { id: "1", event: "a", data: "x" },
      { id: "2", event: "b", data: "y" },
    ]);
  });

  it("joins multi-line data fields with newlines", () => {
    const parser = new SseFrameParser();
    const frames = parser.push("event: a\ndata: line1\ndata: line2\n\n");
    expect(frames).toEqual([{ id: undefined, event: "a", data: "line1\nline2" }]);
  });
});

function streamFromChunks(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

describe("runEventStream", () => {
  it("invalidates the live-query screens' query keys when a frame arrives", async () => {
    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
    const fetchImpl = vi.fn().mockResolvedValue(
      new Response(streamFromChunks(['id: 1\nevent: document.approved\ndata: {}\n\n']), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      }),
    );
    const controller = new AbortController();

    // The stream "closes" after the one frame (ReadableStream ends), which
    // makes runEventStream loop back around to reconnect; abort before the
    // scheduled reconnect actually fires so the test terminates.
    await runEventStream("northstar", queryClient, controller.signal, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onReconnectDelay: () => controller.abort(),
    });

    expect(fetchImpl).toHaveBeenCalledWith(
      expect.stringContaining("/orgs/northstar/events/stream"),
      expect.objectContaining({ signal: controller.signal }),
    );
    for (const prefix of ["documents", "review-tasks", "jobs", "jobs-stats"]) {
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: [prefix, "northstar"] });
    }
  });

  it("resumes with after_id on reconnect after the last seen event", async () => {
    const queryClient = new QueryClient();
    const calls: string[] = [];
    const fetchImpl = vi.fn().mockImplementation((url: string) => {
      calls.push(url);
      if (calls.length === 1) {
        return Promise.resolve(
          new Response(streamFromChunks(["id: evt-42\nevent: a\ndata: {}\n\n"]), { status: 200 }),
        );
      }
      controller.abort();
      return Promise.resolve(new Response(streamFromChunks([]), { status: 200 }));
    });
    const controller = new AbortController();

    await runEventStream("northstar", queryClient, controller.signal, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onReconnectDelay: () => {
        /* let the loop continue to the second connect immediately */
      },
    });

    expect(calls[0]).not.toContain("after_id");
    expect(calls[1]).toContain("after_id=evt-42");
  });

  it("does nothing special on a failed connect other than reconnect with backoff", async () => {
    const queryClient = new QueryClient();
    const fetchImpl = vi.fn().mockResolvedValue(new Response(null, { status: 500 }));
    const controller = new AbortController();
    const delays: number[] = [];

    await runEventStream("northstar", queryClient, controller.signal, {
      fetchImpl: fetchImpl as unknown as typeof fetch,
      onReconnectDelay: (ms) => {
        delays.push(ms);
        if (delays.length >= 2) controller.abort();
      },
    });

    expect(delays[0]).toBe(1_000);
    expect(delays[1]).toBe(2_000); // exponential backoff, capped at 30s
  });
});
