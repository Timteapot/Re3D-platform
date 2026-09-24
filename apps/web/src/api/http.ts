export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function readApiError(response: Response): Promise<ApiError> {
  let message = `请求失败（HTTP ${response.status}）`;
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (typeof payload.detail === "string" && payload.detail.trim()) {
      message = payload.detail;
    }
  } catch {
    // Keep the status-based fallback when the response is not JSON.
  }
  return new ApiError(response.status, message);
}

export function requestResponse(
  path: string,
  init: RequestInit = {},
  accessToken?: string,
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (typeof init.body === "string") {
    headers.set("Content-Type", "application/json");
  }
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
  }

  return fetch(path, {
    ...init,
    credentials: "include",
    headers,
  });
}

export async function requestJson<T>(
  path: string,
  init: RequestInit = {},
  accessToken?: string,
): Promise<T> {
  const response = await requestResponse(path, init, accessToken);

  if (!response.ok) {
    throw await readApiError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}
