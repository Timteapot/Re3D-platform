import { describe, expect, it, vi } from "vitest";

import type { Job } from "./api";
import { parseEventBlock, streamJobEvents } from "./events";

const job: Job = {
  job_id: "a43a63ce-b317-4ef4-bfea-8bff2a5f8341",
  user_id: "9323b31e-a156-4f97-9343-da601cc68fdd",
  status: "succeeded",
  execution_mode: "simulated",
  attempt: 1,
  progress: 100,
  cancel_requested: false,
  error_code: null,
  created_at: "2026-09-24T00:00:00Z",
  queued_at: "2026-09-24T00:00:00Z",
  started_at: "2026-09-24T00:00:01Z",
  finished_at: "2026-09-24T00:00:02Z",
  version: 9,
  reused: false,
};

describe("job event stream", () => {
  it("parses event fields and ignores keep-alive comments", () => {
    expect(parseEventBlock(": keep-alive")).toBeNull();
    expect(parseEventBlock("id: 9\nevent: job\ndata: {\"progress\":100}"))
      .toEqual({ event: "job", data: '{"progress":100}' });
  });

  it("decodes one SSE event even when network chunks split its JSON", async () => {
    const encoded = new TextEncoder().encode(
      `id: 9\nevent: job\ndata: ${JSON.stringify(job)}\n\n`,
    );
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoded.slice(0, 37));
        controller.enqueue(encoded.slice(37));
        controller.close();
      },
    });
    const fetchAuthorized = vi.fn().mockResolvedValue(
      new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );
    const received: Job[] = [];

    await streamJobEvents(
      fetchAuthorized,
      job.job_id,
      (value) => received.push(value),
      new AbortController().signal,
    );

    expect(received).toEqual([job]);
    expect(fetchAuthorized).toHaveBeenCalledWith(
      `/api/v1/development/jobs/${job.job_id}/events`,
      expect.objectContaining({ headers: { Accept: "text/event-stream" } }),
    );
  });
});
