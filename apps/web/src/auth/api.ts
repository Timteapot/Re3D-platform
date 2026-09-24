import type { LoginInput, RegisterInput, TokenResponse, User } from "./types";

const AUTH_BASE = "/api/v1/auth";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${AUTH_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers,
  });

  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string" && payload.detail.trim()) {
        message = payload.detail;
      }
    } catch {
      // Keep the status-based fallback when the response is not JSON.
    }
    throw new ApiError(response.status, message);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function register(input: RegisterInput): Promise<User> {
  return requestJson<User>("/register", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function login(input: LoginInput): Promise<TokenResponse> {
  return requestJson<TokenResponse>("/login", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

let refreshInFlight: Promise<TokenResponse> | null = null;

export function refreshSession(): Promise<TokenResponse> {
  if (refreshInFlight === null) {
    refreshInFlight = requestJson<TokenResponse>("/refresh", {
      method: "POST",
    }).finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

export function logout(): Promise<void> {
  return requestJson<void>("/logout", { method: "POST" });
}
