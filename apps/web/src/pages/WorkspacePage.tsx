import { useAuth } from "../auth/AuthContext";

const stages = ["图片输入", "任务排队", "三分支重建", "自动评估"];

export function WorkspacePage() {
  const auth = useAuth();

  return (
    <main className="page workspace-page">
      <header className="workspace-header">
        <div>
          <p className="eyebrow">RECONSTRUCTION WORKSPACE</p>
          <h1>你好，{auth.user?.username}</h1>
          <p>认证会话已经接通。下一阶段将在此加入图片上传、任务状态、三维查看和评估结果。</p>
        </div>
        <span className="session-state">
          <i aria-hidden="true" /> 会话有效
        </span>
      </header>

      <section className="workspace-grid">
        <article className="workspace-card upload-placeholder">
          <div className="card-label">INPUT</div>
          <h2>准备多视图图片</h2>
          <p>上传接口尚未实现，因此本页不会把本地图片写入临时目录或绕过后端校验。</p>
          <button className="button primary" type="button" disabled>
            上传功能待下一阶段接入
          </button>
        </article>

        <article className="workspace-card">
          <div className="card-label">PIPELINE</div>
          <h2>任务执行阶段</h2>
          <ol className="stage-list">
            {stages.map((stage, index) => (
              <li key={stage}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                {stage}
              </li>
            ))}
          </ol>
        </article>

        <article className="workspace-card wide">
          <div className="card-label">BOUNDARY</div>
          <h2>当前工作台能证明什么</h2>
          <div className="readiness-grid">
            <div><strong>已接通</strong><span>注册、登录、退出、刷新恢复与路由保护</span></div>
            <div><strong>后端已有</strong><span>模拟任务、租约 Worker、三条 GLB 和结构评估</span></div>
            <div><strong>尚待开发</strong><span>上传 API、任务列表、结果下载与 Three.js 查看器</span></div>
          </div>
        </article>
      </section>
    </main>
  );
}
