import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { App } from "./App";
import { ApiError } from "./auth/api";
import { AuthProvider } from "./auth/AuthContext";

const apiMocks = vi.hoisted(() => ({
  confirmEmailVerification: vi.fn(),
  confirmPasswordReset: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  refreshSession: vi.fn(),
  register: vi.fn(),
  requestPasswordReset: vi.fn(),
}));

vi.mock("./auth/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("./auth/api")>();
  return { ...original, ...apiMocks };
});

function renderRoute(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("application authentication routes", () => {
  beforeEach(() => {
    apiMocks.refreshSession.mockRejectedValue(new ApiError(401, "missing refresh token"));
    window.history.replaceState({}, "", "/");
  });

  it("redirects an anonymous workspace visit to login", async () => {
    renderRoute("/workspace");

    expect(await screen.findByRole("heading", { name: "欢迎回来" })).toBeInTheDocument();
    expect(screen.getByLabelText("用户名或邮箱")).toBeInTheDocument();
  });

  it("rejects mismatched registration passwords before calling the API", async () => {
    const user = userEvent.setup();
    renderRoute("/auth/register");
    await screen.findByRole("heading", { name: "建立你的 Re3D 工作区" });

    await user.type(screen.getByLabelText("用户名"), "learner");
    await user.type(screen.getByLabelText("邮箱"), "learner@example.com");
    await user.type(screen.getByLabelText("密码", { exact: true }), "a valid password");
    await user.type(screen.getByLabelText("确认密码"), "a different password");
    await user.click(screen.getByRole("button", { name: "创建账户" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("两次输入的密码不一致");
    expect(apiMocks.register).not.toHaveBeenCalled();
  });

  it("shows a generic result after requesting a password reset", async () => {
    apiMocks.requestPasswordReset.mockResolvedValue({ detail: "accepted" });
    const user = userEvent.setup();
    renderRoute("/auth/forgot-password");

    await user.type(await screen.findByLabelText("注册邮箱"), "learner@example.com");
    await user.click(screen.getByRole("button", { name: "发送重置邮件" }));

    expect(apiMocks.requestPasswordReset).toHaveBeenCalledWith("learner@example.com");
    expect(await screen.findByRole("status")).toHaveTextContent(
      "如果邮箱对应有效账号，将收到密码重置邮件",
    );
  });

  it("confirms an email token from the URL fragment", async () => {
    apiMocks.confirmEmailVerification.mockResolvedValue({
      id: "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
      username: "learner",
      email: "learner@example.com",
      role: "user",
      is_active: true,
      email_verified: true,
      created_at: "2026-09-24T00:00:00Z",
      last_login_at: null,
    });
    window.history.replaceState({}, "", "/#token=verify-token");

    renderRoute("/auth/verify-email");

    expect(await screen.findByRole("status")).toHaveTextContent("邮箱验证成功");
    expect(apiMocks.confirmEmailVerification).toHaveBeenCalledWith("verify-token");
    expect(window.location.hash).toBe("");
  });

  it("rejects mismatched reset passwords without consuming the token", async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, "", "/#token=reset-token");
    renderRoute("/auth/reset-password");

    await user.type(await screen.findByLabelText("新密码"), "a secure password");
    await user.type(screen.getByLabelText("确认新密码"), "a different password");
    await user.click(screen.getByRole("button", { name: "更新密码" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("两次输入的密码不一致");
    expect(apiMocks.confirmPasswordReset).not.toHaveBeenCalled();
  });
});
