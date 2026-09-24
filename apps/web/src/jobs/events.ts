import { readApiError } from "../api/http";
import type { Job } from "./api";

export type AuthorizedFetch = (
  path: string,
  init?: RequestInit,
) => Promise<Response>;

interface ParsedEvent {
  event: string;
  data: string;
}

export function parseEventBlock(block: string): ParsedEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const rawLine of block.split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(":")) continue;
    const separator = rawLine.indexOf(":");
    const field = separator === -1 ? rawLine : rawLine.slice(0, separator);
    let value = separator === -1 ? "" : rawLine.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  }
  return data.length ? { event, data: data.join("\n") } : null;
}

export async function streamJobEvents(
  fetchAuthorized: AuthorizedFetch,
  jobId: string,
  onJob: (job: Job) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetchAuthorized(
    `/api/v1/development/jobs/${jobId}/events`,
    { headers: { Accept: "text/event-stream" }, signal },
  );
  if (!response.ok) throw await readApiError(response);
  if (!response.body) throw new Error("浏览器未提供可读取的状态流");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const parsed = parseEventBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (parsed?.event === "job") onJob(JSON.parse(parsed.data) as Job);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) return;
  }
}
