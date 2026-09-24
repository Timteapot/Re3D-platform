import { useState, type FormEvent } from "react";
import { Link, Navigate, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../auth/api";
import { useAuth } from "../auth/AuthContext";
import { safeReturnPath } from "../auth/navigation";

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "用户名、邮箱或密码不正确。";
    if (error.status === 409) return "用户名或邮箱已经被注册。";
    if (error.status === 422) return `输入未通过校验：${error.message}`;
    return error.message;
  }
  return "认证服务暂时不可用，请稍后重试。";
}

export function AuthPage() {
  const { mode } = useParams();
  const isRegister = mode === "register";
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");

  if (mode !== "login" && mode !== "register") {
    return <Navigate to="/auth/login" replace />;
  }
  if (auth.status === "authenticated") {
    return <Navigate to="/workspace" replace />;
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    const data = new FormData(event.currentTarget);

    if (isRegister && password !== confirmation) {
      setError("两次输入的密码不一致。");
      return;
    }

    setSubmitting(true);
    try {
      if (isRegister) {
        await auth.register({
          username: String(data.get("username") ?? ""),
          email: String(data.get("email") ?? ""),
          password,
        });
        navigate("/auth/login", {
          replace: true,
          state: { notice: "注册成功，请使用新账号登录。" },
        });
      } else {
        await auth.login({
          identifier: String(data.get("identifier") ?? ""),
          password,
        });
        navigate(safeReturnPath(searchParams.get("returnTo")), { replace: true });
      }
    } catch (requestError) {
      setError(messageFor(requestError));
    } finally {
      setSubmitting(false);
    }
  }

  const locationState = location.state as { notice?: unknown } | null;
  const notice = typeof locationState?.notice === "string" ? locationState.notice : null;

  return (
    <main className="auth-layout">
      <section className="auth-context" aria-label="认证说明">
        <Link className="back-link" to="/">
          ← 返回首页
        </Link>
        <div>
          <p className="eyebrow">ACCOUNT GATEWAY</p>
          <h1>{isRegister ? "创建学习账户" : "继续你的重建任务"}</h1>
          <p>
            Access Token 只保存在当前页面内存中；用于恢复会话的 Refresh Token 由浏览器以 HttpOnly Cookie 管理，页面脚本不能读取。
          </p>
        </div>
        <ul className="security-list">
          <li>密码使用 Argon2id 哈希</li>
          <li>会话刷新令牌轮换并检测重放</li>
          <li>任务所有权由后端令牌身份决定</li>
        </ul>
      </section>

      <section className="auth-panel">
        <div className="auth-card">
          <div className="auth-heading">
            <p>{isRegister ? "注册" : "登录"}</p>
            <h2>{isRegister ? "建立你的 Re3D 工作区" : "欢迎回来"}</h2>
          </div>

          {notice ? <div className="form-notice success">{notice}</div> : null}
          {error ? <div className="form-notice error" role="alert">{error}</div> : null}

          <form onSubmit={(event) => void submit(event)}>
            {isRegister ? (
              <>
                <label htmlFor="username">用户名</label>
                <input
                  autoComplete="username"
                  id="username"
                  name="username"
                  minLength={3}
                  maxLength={32}
                  pattern="[A-Za-z0-9][A-Za-z0-9_.-]{2,31}"
                  required
                />
                <p className="field-hint">3–32 位字母、数字、点、下划线或连字符</p>

                <label htmlFor="email">邮箱</label>
                <input autoComplete="email" id="email" name="email" type="email" maxLength={320} required />
              </>
            ) : (
              <>
                <label htmlFor="identifier">用户名或邮箱</label>
                <input autoComplete="username" id="identifier" name="identifier" maxLength={320} required />
              </>
            )}

            <label htmlFor="password">密码</label>
            <input
              autoComplete={isRegister ? "new-password" : "current-password"}
              id="password"
              name="password"
              type="password"
              minLength={isRegister ? 12 : 1}
              maxLength={128}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            {isRegister ? <p className="field-hint">至少 12 个非空白字符</p> : null}

            {isRegister ? (
              <>
                <label htmlFor="confirmation">确认密码</label>
                <input
                  autoComplete="new-password"
                  id="confirmation"
                  name="confirmation"
                  type="password"
                  minLength={12}
                  maxLength={128}
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  required
                />
              </>
            ) : null}

            <button className="button primary full" type="submit" disabled={submitting}>
              {submitting ? "正在提交…" : isRegister ? "创建账户" : "登录"}
            </button>
          </form>

          <p className="auth-switch">
            {isRegister ? "已有账户？" : "还没有账户？"}
            <Link to={isRegister ? "/auth/login" : "/auth/register"}>
              {isRegister ? "直接登录" : "创建账户"}
            </Link>
          </p>
        </div>
      </section>
    </main>
  );
}
