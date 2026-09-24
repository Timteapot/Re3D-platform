import { Link } from "react-router-dom";

const pipeline = [
  { key: "01", title: "提交多视图图片", detail: "任务级隔离保存，进入队列前完成格式与完整性检查。" },
  { key: "02", title: "执行固定三分支", detail: "A-v4、B-v2、C 由独立 Worker 串行占用 GPU。" },
  { key: "03", title: "查看模型与评估", detail: "对每条产物展示结构检查和后续质量评估结果。" },
];

export function HomePage() {
  return (
    <main>
      <section className="hero page">
        <div className="hero-copy">
          <p className="eyebrow">IMAGE TO SPATIAL RESULT</p>
          <h1>
            从多视图图片到
            <span>可检查的三维结果</span>
          </h1>
          <p className="hero-lead">
            Re3D Platform 将固定版本的重建管线、异步任务、三分支产物和自动评估组织成一个可追踪的学习型工程系统。
          </p>
          <div className="hero-actions">
            <Link className="button primary" to="/workspace">
              进入重建工作台
            </Link>
            <Link className="button secondary" to="/about">
              了解技术边界
            </Link>
          </div>
          <dl className="hero-facts">
            <div>
              <dt>3</dt>
              <dd>固定重建分支</dd>
            </div>
            <div>
              <dt>1</dt>
              <dd>任务级数据边界</dd>
            </div>
            <div>
              <dt>0</dt>
              <dd>虚构质量分数</dd>
            </div>
          </dl>
        </div>
        <div className="hero-visual" aria-label="三条重建分支流程示意">
          <div className="mesh-orbit orbit-one" />
          <div className="mesh-orbit orbit-two" />
          <div className="mesh-core">Re3D</div>
          <span className="branch-label branch-a">A-v4</span>
          <span className="branch-label branch-b">B-v2</span>
          <span className="branch-label branch-c">C</span>
        </div>
      </section>

      <section className="page process-section" aria-labelledby="pipeline-title">
        <div className="section-heading">
          <p className="eyebrow">CONTROLLED PIPELINE</p>
          <h2 id="pipeline-title">一次任务，一条完整证据链</h2>
        </div>
        <div className="process-grid">
          {pipeline.map((item) => (
            <article className="process-card" key={item.key}>
              <span>{item.key}</span>
              <h3>{item.title}</h3>
              <p>{item.detail}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="page boundary-panel">
        <div>
          <p className="eyebrow">CURRENT BOUNDARY</p>
          <h2>目前是开发验证系统，还不是公开生产服务</h2>
        </div>
        <p>
          认证、数据库队列和模拟重建闭环已经可运行；真实 GPU 执行、邮件验证、限流、数据保留策略和生产部署仍需继续完成。
        </p>
      </section>
    </main>
  );
}
