import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, login, refreshSession } from "./api";

const session = {
  access_token: "access-token",
  token_type: "bearer" as const,
  expires_in: 900,
  user: {
    id: "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    username: "learner",
    email: "learner@example.com",
    role: "user",
    is_active: true,
    email_verified: false,
    created_at: "2026-09-24T00:00:00Z",
    last_login_at: null,
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("authentication API", () => {
  it("sends JSON login credentials with browser cookies enabled", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(session), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(login({ identifier: "learner", password: "test password" })).resolves.toEqual(session);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/login",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ identifier: "learner", password: "test password" }),
      }),
    );
  });

  it("coalesces concurrent refresh requests", async () => {
    let resolveResponse: ((value: Response) => void) | undefined;
    const fetchMock = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveResponse = resolve;
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const first = refreshSession();
    const second = refreshSession();
    expect(first).toBe(second);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    resolveResponse?.(
      new Response(JSON.stringify(session), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await expect(first).resolves.toEqual(session);
  });

  it("exposes the backend detail on an unsuccessful response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "invalid credentials" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const request = login({ identifier: "learner", password: "wrong" });
    await expect(request).rejects.toBeInstanceOf(ApiError);
    await expect(request).rejects.toMatchObject({
      status: 401,
      message: "invalid credentials",
    });
  });
});
