import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import {
  ApiError,
  confirmPasswordReset,
  requestPasswordReset,
} from "../auth/api";
import { useAuth } from "../auth/AuthContext";

type RecoveryMode = "verify-email" | "forgot-password" | "reset-password";

function fragmentToken(): string | null {
  const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
  return token?.trim() || null;
}

function clearFragment(): void {
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
}

function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 400) return "链接无效、已过期或已经使用，请重新发起请求。";
    if (error.status === 422) return `输入未通过校验：${error.message}`;
    if (error.status === 429) return "请求过于频繁，请稍后重试。";
    return error.message;
  }
  return "认证服务暂时不可用，请稍后重试。";
}

export function AccountRecoveryPage({ mode }: { mode: RecoveryMode }) {
  const auth = useAuth();
  const initialToken = useRef(fragmentToken());
  const verificationStarted = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [completed, setCompleted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");

  useEffect(() => {
    if (mode !== "verify-email" || verificationStarted.current) return;
    verificationStarted.current = true;
    const token = initialToken.current;
    if (token === null) {
      setError("验证链接缺少令牌，请在工作区重新发送验证邮件。");
      return;
    }
    setSubmitting(true);
    void auth
      .confirmEmailVerification(token)
      .then(() => {
        clearFragment();
        setCompleted(true);
      })
      .catch((requestError: unknown) => setError(messageFor(requestError)))
      .finally(() => setSubmitting(false));
  }, [auth, mode]);

  async function submitForgotPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    const data = new FormData(event.currentTarget);
    try {
      await requestPasswordReset(String(data.get("email") ?? ""));
      setCompleted(true);
    } catch (requestError) {
      setError(messageFor(requestError));
    } finally {
      setSubmitting(false);
    }
  }

  async function submitPasswordReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (password !== confirmation) {
      setError("两次输入的密码不一致。");
      return;
    }
    const token = initialToken.current;
    if (token === null) {
      setError("重置链接缺少令牌，请重新申请密码重置邮件。");
      return;
    }
    setSubmitting(true);
    try {
      await confirmPasswordReset(token, password);
      await auth.logout().catch(() => undefined);
      clearFragment();
      setCompleted(true);
    } catch (requestError) {
      setError(messageFor(requestError));
    } finally {
      setSubmitting(false);
    }
  }

  const content = {
    "verify-email": {
      eyebrow: "EMAIL VERIFICATION",
      title: "确认邮箱所有权",
      description: "验证成功后，账号才可以上传图片并创建重建任务。",
      heading: "正在验证邮箱",
    },
    "forgot-password": {
      eyebrow: "PASSWORD RECOVERY",
      title: "找回账户访问权",
      description: "系统不会透露邮箱是否已注册；有效账号将收到一次性重置链接。",
      heading: "申请密码重置",
    },
    "reset-password": {
      eyebrow: "PASSWORD RESET",
      title: "设置新的登录密码",
      description: "重置成功后，现有刷新会话会全部失效，需要使用新密码重新登录。",
      heading: "更新密码",
    },
  }[mode];

  return (
    <main className="auth-layout">
      <section className="auth-context" aria-label="账户恢复说明">
        <Link className="back-link" to="/">
          ← 返回首页
        </Link>
        <div>
          <p className="eyebrow">{content.eyebrow}</p>
          <h1>{content.title}</h1>
          <p>{content.description}</p>
        </div>
        <ul className="security-list">
          <li>令牌只放在链接 fragment 中</li>
          <li>令牌以摘要形式存储且只能使用一次</li>
          <li>验证与重置操作均写入安全审计事件</li>
        </ul>
      </section>

      <section className="auth-panel">
        <div className="auth-card">
          <div className="auth-heading">
            <p>ACCOUNT SECURITY</p>
            <h2>{content.heading}</h2>
          </div>

          {error ? <div className="form-notice error" role="alert">{error}</div> : null}

          {mode === "verify-email" ? (
            completed ? (
              <div className="recovery-result">
                <div className="form-notice success" role="status">邮箱验证成功。</div>
                <Link className="button primary full" to={auth.status === "authenticated" ? "/workspace" : "/auth/login"}>
                  {auth.status === "authenticated" ? "返回工作区" : "前往登录"}
                </Link>
              </div>
            ) : (
              <p className="recovery-status">{submitting ? "正在校验一次性令牌…" : "无法完成验证。"}</p>
            )
          ) : null}

          {mode === "forgot-password" ? (
            completed ? (
              <div className="recovery-result">
                <div className="form-notice success" role="status">
                  请求已受理。如果邮箱对应有效账号，将收到密码重置邮件。
                </div>
                <Link className="text-link" to="/auth/login">返回登录</Link>
              </div>
            ) : (
              <form onSubmit={(event) => void submitForgotPassword(event)}>
                <label htmlFor="recovery-email">注册邮箱</label>
                <input autoComplete="email" id="recovery-email" name="email" type="email" maxLength={320} required />
                <button className="button primary full" type="submit" disabled={submitting}>
                  {submitting ? "正在提交…" : "发送重置邮件"}
                </button>
              </form>
            )
          ) : null}

          {mode === "reset-password" ? (
            completed ? (
              <div className="recovery-result">
                <div className="form-notice success" role="status">密码已更新，请使用新密码登录。</div>
                <Link className="button primary full" to="/auth/login">前往登录</Link>
              </div>
            ) : (
              <form onSubmit={(event) => void submitPasswordReset(event)}>
                <label htmlFor="new-password">新密码</label>
                <input
                  autoComplete="new-password"
                  id="new-password"
                  minLength={12}
                  maxLength={128}
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
                <p className="field-hint">至少 12 个非空白字符</p>
                <label htmlFor="new-password-confirmation">确认新密码</label>
                <input
                  autoComplete="new-password"
                  id="new-password-confirmation"
                  minLength={12}
                  maxLength={128}
                  type="password"
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  required
                />
                <button className="button primary full" type="submit" disabled={submitting}>
                  {submitting ? "正在更新…" : "更新密码"}
                </button>
              </form>
            )
          ) : null}

          {mode !== "verify-email" && !completed ? (
            <p className="auth-switch"><Link to="/auth/login">返回登录</Link></p>
          ) : null}
        </div>
      </section>
    </main>
  );
}
