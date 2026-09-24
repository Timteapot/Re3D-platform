import { Navigate, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "./AuthContext";

export function ProtectedRoute() {
  const auth = useAuth();
  const location = useLocation();

  if (auth.status === "loading") {
    return (
      <main className="page narrow-page" aria-live="polite">
        <div className="status-card">
          <span className="spinner" aria-hidden="true" />
          <div>
            <h1>正在恢复会话</h1>
            <p>浏览器正在使用受保护的刷新 Cookie 获取短期访问令牌。</p>
          </div>
        </div>
      </main>
    );
  }

  if (auth.status !== "authenticated") {
    const returnTo = `${location.pathname}${location.search}`;
    return <Navigate to={`/auth/login?returnTo=${encodeURIComponent(returnTo)}`} replace />;
  }

  return <Outlet />;
}
