const branches = [
  ["A-v4", "地图与几何推理分支", "输出受控 GLB，后续接入真实深度和融合指标。"],
  ["B-v2", "第二条独立重建分支", "保留独立产物和评估证据，不覆盖其他分支。"],
  ["C", "第三条固定基线分支", "与 A、B 使用同一任务 UUID 和输入清单。"],
];

export function AboutPage() {
  return (
    <main className="page content-page">
      <header className="page-intro">
        <p className="eyebrow">PROJECT & TECHNOLOGY</p>
        <h1>项目说明与使用边界</h1>
        <p>
          本项目用于学习和实践真实软件项目流程，不用于商业用途，也不承诺重建结果适合测量、工程决策或安全关键场景。
        </p>
      </header>

      <section className="content-section">
        <div className="section-index">01</div>
        <div>
          <h2>系统如何工作</h2>
          <p>
            浏览器只与 FastAPI 通信。API 保存用户和任务元数据，独立 Worker 取得数据库租约后调用固定版本的 Re3D 管线。图片、运行中间文件、日志、三个分支产物和评估报告都限制在单个任务目录内。
          </p>
          <div className="branch-grid">
            {branches.map(([name, title, detail]) => (
              <article key={name}>
                <span>{name}</span>
                <h3>{title}</h3>
                <p>{detail}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="content-section">
        <div className="section-index">02</div>
        <div>
          <h2>建议的图片输入</h2>
          <ol className="instruction-list">
            <li>围绕静止物体或场景，从不同方向拍摄至少三张有重叠区域的图片。</li>
            <li>尽量保持曝光和焦距稳定，避免强反光、透明表面、运动模糊及大面积纯色。</li>
            <li>上传前移除无关或敏感内容；公开部署前的数据保留期限尚未确定。</li>
            <li>提交后通过任务状态查看排队、执行、三分支结果和评估说明。</li>
          </ol>
        </div>
      </section>

      <section className="content-section" id="third-party">
        <div className="section-index">03</div>
        <div>
          <h2>第三方引用与非商业说明</h2>
          <p>
            平台保留 Re3D 及其依赖的原始许可证和引用要求。平台直接使用的 Python 与 JavaScript 软件包也分别按其 MIT、BSD、Apache、LGPL 或其他许可证声明。完整清单随源码仓库维护，后续加入依赖时同步更新。
          </p>
          <a
            className="text-link"
            href="https://github.com/Timteapot/Re3D-platform/blob/main/THIRD_PARTY_NOTICES.md"
            rel="noreferrer"
            target="_blank"
          >
            查看第三方声明源文件
          </a>
        </div>
      </section>
    </main>
  );
}
