import type { LoginInput, RegisterInput, TokenResponse, User } from "./types";
import { requestJson } from "../api/http";

const AUTH_BASE = "/api/v1/auth";

export { ApiError } from "../api/http";

export function register(input: RegisterInput): Promise<User> {
  return requestJson<User>(`${AUTH_BASE}/register`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function login(input: LoginInput): Promise<TokenResponse> {
  return requestJson<TokenResponse>(`${AUTH_BASE}/login`, {
    method: "POST",
    body: JSON.stringify(input),
  });
}

let refreshInFlight: Promise<TokenResponse> | null = null;

export function refreshSession(): Promise<TokenResponse> {
  if (refreshInFlight === null) {
    refreshInFlight = requestJson<TokenResponse>(`${AUTH_BASE}/refresh`, {
      method: "POST",
    }).finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

export function logout(): Promise<void> {
  return requestJson<void>(`${AUTH_BASE}/logout`, { method: "POST" });
}
