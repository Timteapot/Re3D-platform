import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { App } from "./App";
import { ApiError } from "./auth/api";
import { AuthProvider } from "./auth/AuthContext";

const apiMocks = vi.hoisted(() => ({
  login: vi.fn(),
  logout: vi.fn(),
  refreshSession: vi.fn(),
  register: vi.fn(),
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
});
