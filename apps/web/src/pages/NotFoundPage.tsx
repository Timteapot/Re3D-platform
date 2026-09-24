import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <main className="page narrow-page">
      <div className="not-found">
        <p className="eyebrow">404 / ROUTE NOT FOUND</p>
        <h1>这个页面不存在</h1>
        <p>地址可能已经改变，或者功能尚未加入当前开发版本。</p>
        <Link className="button primary" to="/">返回首页</Link>
      </div>
    </main>
  );
}
