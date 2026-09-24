import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/AuthContext";

function navClass({ isActive }: { isActive: boolean }) {
  return isActive ? "nav-link active" : "nav-link";
}

export function AppShell() {
  const auth = useAuth();

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <header className="site-header">
        <NavLink className="brand" to="/" aria-label="Re3D Platform 首页">
          <span className="brand-mark" aria-hidden="true">
            R3
          </span>
          <span>
            <strong>Re3D</strong>
            <small>Reconstruction Lab</small>
          </span>
        </NavLink>
        <nav className="primary-nav" aria-label="主导航">
          <NavLink className={navClass} to="/" end>
            首页
          </NavLink>
          <NavLink className={navClass} to="/about">
            项目说明
          </NavLink>
          <NavLink className={navClass} to="/workspace">
            重建工作台
          </NavLink>
        </nav>
        <div className="account-actions">
          {auth.status === "authenticated" && auth.user ? (
            <>
              <span className="user-chip">{auth.user.username}</span>
              <button className="button ghost small" type="button" onClick={() => void auth.logout()}>
                退出
              </button>
            </>
          ) : (
            <>
              <NavLink className="button ghost small" to="/auth/login">
                登录
              </NavLink>
              <NavLink className="button primary small" to="/auth/register">
                注册
              </NavLink>
            </>
          )}
        </div>
      </header>

      {auth.restoreError ? (
        <div className="service-banner" role="status">
          <span>{auth.restoreError}</span>
          <button type="button" onClick={() => void auth.retryRestore()}>
            重试
          </button>
        </div>
      ) : null}

      <div id="main-content">
        <Outlet />
      </div>

      <footer className="site-footer">
        <p>Re3D Platform · 非商业学习与工程实践项目</p>
        <NavLink to="/about#third-party">第三方声明</NavLink>
      </footer>
    </div>
  );
}
