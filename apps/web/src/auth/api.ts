import type { LoginInput, RegisterInput, TokenResponse, User } from "./types";
import { requestJson } from "../api/http";

const AUTH_BASE = "/api/v1/auth";

export interface AcceptedResponse {
  detail: string;
}

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

export function requestEmailVerification(
  accessToken: string,
): Promise<AcceptedResponse> {
  return requestJson<AcceptedResponse>(
    `${AUTH_BASE}/email-verification/request`,
    { method: "POST" },
    accessToken,
  );
}

export function confirmEmailVerification(token: string): Promise<User> {
  return requestJson<User>(`${AUTH_BASE}/email-verification/confirm`, {
    method: "POST",
    body: JSON.stringify({ token }),
  });
}

export function requestPasswordReset(email: string): Promise<AcceptedResponse> {
  return requestJson<AcceptedResponse>(`${AUTH_BASE}/password-reset/request`, {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export function confirmPasswordReset(
  token: string,
  newPassword: string,
): Promise<void> {
  return requestJson<void>(`${AUTH_BASE}/password-reset/confirm`, {
    method: "POST",
    body: JSON.stringify({ token, new_password: newPassword }),
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
