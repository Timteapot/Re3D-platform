import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "../api/http";
import { useAuth } from "../auth/AuthContext";
import { cancelJob, getJobDetail, type Job, type JobDetail } from "../jobs/api";
import { streamJobEvents } from "../jobs/events";

const TERMINAL_STATUSES = new Set([
  "succeeded",
  "failed_input",
  "failed_pipeline",
  "failed_evaluation",
  "cancelled",
  "expired",
]);

const statusLabels: Record<string, string> = {
  queued: "排队中",
  preparing: "准备中",
  sfm: "相机位姿",
  dense_reconstruction: "稠密重建",
  meshing: "网格生成",
  texturing: "纹理生成",
  validating_output: "产物校验",
  evaluating: "自动评估",
  succeeded: "已完成",
  cancelled: "已取消",
  expired: "已过期",
  failed_input: "输入失败",
  failed_pipeline: "管线失败",
  failed_evaluation: "评估失败",
};

const assessmentLabels: Record<string, string> = {
  pass: "通过",
  warning: "需关注",
  fail: "未通过",
  not_available: "不可评估",
  not_applicable: "不适用",
};

const warningLabels: Record<string, string> = {
  TASK_DIRECTORY_MISSING: "任务目录不存在，数据库状态与文件存储不一致。",
  TASK_CONTRACT_INVALID: "任务文件未通过契约或身份一致性校验，详情已停止展示。",
  TERMINAL_REPORT_MISSING: "任务已标记完成，但结果或评估报告缺失。",
};

function readableError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "无法读取任务详情，请检查 API 与网络连接。";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

function applyJobUpdate(detail: JobDetail | undefined, job: Job): JobDetail | undefined {
  return detail ? { ...detail, job } : detail;
}

export function JobDetailPage() {
  const { jobId = "" } = useParams();
  const auth = useAuth();
  const queryClient = useQueryClient();
  const queryKey = ["job-detail", auth.user?.id, jobId] as const;
  const [streamState, setStreamState] = useState<"connecting" | "live" | "fallback" | "closed">("connecting");

  const detail = useQuery({
    queryKey,
    queryFn: () => getJobDetail(auth.request, jobId),
    enabled: auth.status === "authenticated" && jobId.length > 0,
    refetchInterval: (query) => {
      const job = query.state.data?.job;
      return job && !TERMINAL_STATUSES.has(job.status) ? 5000 : false;
    },
  });

  const cancel = useMutation({
    mutationFn: () => cancelJob(auth.request, jobId),
    onSuccess: (job) => {
      queryClient.setQueryData<JobDetail | undefined>(queryKey, (current) =>
        applyJobUpdate(current, job),
      );
      void queryClient.invalidateQueries({ queryKey: ["jobs", auth.user?.id] });
      void queryClient.invalidateQueries({ queryKey });
    },
  });

  const currentStatus = detail.data?.job.status;
  useEffect(() => {
    if (
      auth.status !== "authenticated" ||
      !jobId ||
      !currentStatus ||
      TERMINAL_STATUSES.has(currentStatus)
    ) {
      setStreamState("closed");
      return;
    }
    const controller = new AbortController();
    setStreamState("connecting");
    void streamJobEvents(
      auth.fetchAuthorized,
      jobId,
      (job) => {
        setStreamState("live");
        queryClient.setQueryData<JobDetail | undefined>(queryKey, (current) =>
          applyJobUpdate(current, job),
        );
        if (TERMINAL_STATUSES.has(job.status)) {
          setStreamState("closed");
          void queryClient.invalidateQueries({ queryKey });
          void queryClient.invalidateQueries({ queryKey: ["jobs", auth.user?.id] });
        }
      },
      controller.signal,
    ).catch((error: unknown) => {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        setStreamState("fallback");
      }
    });
    return () => controller.abort();
  }, [auth.fetchAuthorized, auth.status, auth.user?.id, currentStatus, jobId, queryClient]);

  if (detail.isLoading) {
    return (
      <main className="page narrow-page">
        <div className="status-card"><span className="spinner" /><div><h1>读取任务</h1><p>正在核对状态和任务契约。</p></div></div>
      </main>
    );
  }
  if (detail.isError || !detail.data) {
    return (
      <main className="page narrow-page">
        <div className="status-card"><div><h1>无法显示任务</h1><p>{readableError(detail.error)}</p><Link className="text-link" to="/workspace">返回工作台</Link></div></div>
      </main>
    );
  }

  const value = detail.data;
  const job = value.job;
  const active = !TERMINAL_STATUSES.has(job.status);
  const connectionLabel = streamState === "live"
    ? "实时状态已连接"
    : streamState === "fallback"
      ? "实时连接中断，5 秒轮询中"
      : streamState === "connecting"
        ? "正在连接实时状态"
        : "任务状态已终止";

  return (
    <main className="page job-detail-page">
      <Link className="back-link job-back" to="/workspace">← 返回重建工作台</Link>
      <header className="job-detail-header">
        <div>
          <p className="eyebrow">JOB DETAIL · {job.execution_mode.toUpperCase()}</p>
          <h1>{statusLabels[job.status] ?? job.status}</h1>
          <code>{job.job_id}</code>
        </div>
        <div className="job-live-state">
          <span className={`live-dot ${streamState}`} aria-hidden="true" />
          <span>{connectionLabel}</span>
        </div>
      </header>

      {job.execution_mode === "simulated" ? (
        <div className="simulation-banner" role="note">
          当前展示的是开发模拟器输出：它验证队列、文件契约和评估链路，不代表真实三维重建质量。
        </div>
      ) : null}
      {value.warning_code ? (
        <div className="form-notice error" role="alert">
          {warningLabels[value.warning_code] ?? "任务详情存在一致性问题。"}
        </div>
      ) : null}
      {cancel.error ? <div className="form-notice error">{readableError(cancel.error)}</div> : null}

      <section className="job-overview-grid" aria-label="任务概况">
        <article><span>进度</span><strong>{job.progress}%</strong><progress max={100} value={job.progress}>{job.progress}%</progress></article>
        <article><span>输入图片</span><strong>{value.input?.image_count ?? "—"}</strong><small>{value.input ? formatBytes(value.input.total_bytes) : "等待输入摘要"}</small></article>
        <article><span>创建时间</span><strong>{formatDate(job.created_at)}</strong><small>开始：{formatDate(job.started_at)}</small></article>
        <article><span>执行尝试</span><strong>#{job.attempt}</strong><small>状态版本 {job.version}</small></article>
      </section>

      {active ? (
        <section className="job-action-panel">
          <div><h2>任务仍在执行</h2><p>取消排队任务会立即终止；运行中的任务将在 Worker 下一次心跳时协作停止。</p></div>
          <button
            className="button danger"
            type="button"
            disabled={cancel.isPending || job.cancel_requested}
            onClick={() => {
              if (window.confirm("确定取消这个任务吗？已生成的任务文件暂不自动删除。")) cancel.mutate();
            }}
          >
            {job.cancel_requested ? "已请求取消" : cancel.isPending ? "正在取消…" : "取消任务"}
          </button>
        </section>
      ) : null}

      <section className="detail-section">
        <div className="detail-heading"><span>01</span><div><h2>三分支输出</h2><p>仅显示契约内的产物类型、大小和结构指标；文件下载将在后续独立实现授权接口。</p></div></div>
        {value.result ? (
          <div className="result-branch-grid">
            {value.result.branches.map((branch) => (
              <article key={branch.name}>
                <div><span>{branch.name}</span><strong>{statusLabels[branch.status] ?? branch.status}</strong></div>
                <dl>
                  <div><dt>产物</dt><dd>{branch.artifacts.length} 个</dd></div>
                  <div><dt>体积</dt><dd>{formatBytes(branch.artifacts.reduce((sum, item) => sum + item.size_bytes, 0))}</dd></div>
                  <div><dt>顶点 / 面</dt><dd>{String(branch.metrics.vertices ?? "—")} / {String(branch.metrics.faces ?? "—")}</dd></div>
                </dl>
              </article>
            ))}
          </div>
        ) : (
          <p className="empty-state">{active ? "结果尚未生成。页面会随任务状态自动更新。" : "该任务没有可展示的结果摘要。"}</p>
        )}
      </section>

      <section className="detail-section">
        <div className="detail-heading"><span>02</span><div><h2>自动评估</h2><p>首期评估描述结构健康度；没有真值数据时，不把完整性检查包装成几何精度分数。</p></div></div>
        {value.evaluation ? (
          <div className="evaluation-panel">
            <div className="evaluation-summary">
              <span>{assessmentLabels[value.evaluation.overall_status] ?? value.evaluation.overall_status}</span>
              <strong>{value.evaluation.score === null ? "不评分" : value.evaluation.score.toFixed(1)}</strong>
              <p>{value.evaluation.summary}</p>
              <small>规则 {value.evaluation.rules_version} · {value.evaluation.scope}</small>
            </div>
            <div className="evaluation-branches">
              {value.evaluation.branches.map((branch) => (
                <article key={branch.name}>
                  <div><strong>{branch.name}</strong><span>{assessmentLabels[branch.status] ?? branch.status}</span></div>
                  <p>{branch.summary}</p>
                  <small>产物完整性：{branch.artifact_status ? assessmentLabels[branch.artifact_status] ?? branch.artifact_status : "未报告"}</small>
                </article>
              ))}
            </div>
            <ul className="limitations">
              {value.evaluation.limitations.map((item) => <li key={item}>{item}</li>)}
            </ul>
          </div>
        ) : (
          <p className="empty-state">{active ? "评估将在重建输出完成后生成。" : "该任务没有可展示的评估报告。"}</p>
        )}
      </section>
    </main>
  );
}
