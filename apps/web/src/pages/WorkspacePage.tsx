import { useMemo, useState, type ChangeEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "../api/http";
import { useAuth } from "../auth/AuthContext";
import {
  createUpload,
  listJobs,
  submitUpload,
  uploadImage,
  type Job,
} from "../jobs/api";

const MAX_IMAGES = 150;
const MAX_FILE_BYTES = 25 * 1024 * 1024;
const MAX_TOTAL_BYTES = 1024 * 1024 * 1024;
const ACTIVE_STATUSES = new Set([
  "queued",
  "preparing",
  "sfm",
  "dense_reconstruction",
  "meshing",
  "texturing",
  "validating_output",
  "evaluating",
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
  failed_input: "输入失败",
  failed_pipeline: "管线失败",
  failed_evaluation: "评估失败",
};

function readableError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 413) return "图片或任务总大小超过后端限制。";
    if (error.status === 409) return `上传冲突：${error.message}`;
    if (error.status === 422) return `图片未通过校验：${error.message}`;
    return error.message;
  }
  return "无法连接任务服务，请检查 API 和数据库是否已经启动。";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function WorkspacePage() {
  const auth = useAuth();
  const queryClient = useQueryClient();
  const [files, setFiles] = useState<File[]>([]);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [uploadedCount, setUploadedCount] = useState(0);
  const [latestJob, setLatestJob] = useState<Job | null>(null);

  const totalBytes = useMemo(
    () => files.reduce((total, file) => total + file.size, 0),
    [files],
  );

  const jobs = useQuery({
    queryKey: ["jobs", auth.user?.id],
    queryFn: () => listJobs(auth.request),
    enabled: auth.status === "authenticated",
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.some((job) => ACTIVE_STATUSES.has(job.status)) ? 3000 : false;
    },
  });

  const reconstruction = useMutation({
    mutationFn: async (selected: File[]) => {
      setUploadedCount(0);
      const upload = await createUpload(auth.request, crypto.randomUUID());
      for (const [index, file] of selected.entries()) {
        await uploadImage(auth.request, upload.upload_id, file);
        setUploadedCount(index + 1);
      }
      return submitUpload(auth.request, upload.upload_id);
    },
    onSuccess: (job) => {
      setLatestJob(job);
      setFiles([]);
      void queryClient.invalidateQueries({ queryKey: ["jobs", auth.user?.id] });
    },
  });

  function selectFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []);
    reconstruction.reset();
    setLatestJob(null);
    setUploadedCount(0);
    if (selected.length > MAX_IMAGES) {
      setFiles([]);
      setSelectionError(`一次最多选择 ${MAX_IMAGES} 张图片。`);
      event.target.value = "";
      return;
    }
    const oversized = selected.find((file) => file.size > MAX_FILE_BYTES);
    if (oversized) {
      setFiles([]);
      setSelectionError(`${oversized.name} 超过单张 25 MB 的开发限制。`);
      event.target.value = "";
      return;
    }
    const selectedBytes = selected.reduce((total, file) => total + file.size, 0);
    if (selectedBytes > MAX_TOTAL_BYTES) {
      setFiles([]);
      setSelectionError("所选图片合计超过 1 GiB 的开发限制。");
      event.target.value = "";
      return;
    }
    setFiles(selected);
    setSelectionError(
      selected.length > 0 && selected.length < 3
        ? "至少需要选择 3 张具有重叠区域的图片。"
        : null,
    );
  }

  const canSubmit = files.length >= 3 && !reconstruction.isPending;
  const progress = files.length > 0 ? Math.round((uploadedCount / files.length) * 100) : 0;
  const displayedLatestJob = latestJob
    ? jobs.data?.find((job) => job.job_id === latestJob.job_id) ?? latestJob
    : null;

  return (
    <main className="page workspace-page">
      <header className="workspace-header">
        <div>
          <p className="eyebrow">RECONSTRUCTION WORKSPACE</p>
          <h1>你好，{auth.user?.username}</h1>
          <p>上传真实 JPEG/PNG 输入并创建开发任务。当前 Worker 仍生成模拟三分支结果，不代表真实 GPU 重建已经执行。</p>
        </div>
        <span className="session-state">
          <i aria-hidden="true" /> 会话有效
        </span>
      </header>

      <section className="workspace-grid">
        <article className="workspace-card upload-card">
          <div className="card-label">INPUT</div>
          <h2>选择多视图图片</h2>
          <p>支持 JPEG、PNG，3–150 张，单张最大 25 MB、合计最大 1 GiB。后端会重新识别格式、完整解码并校验像素数量。</p>
          <label className="file-picker" htmlFor="reconstruction-images">
            <span>选择图片</span>
            <small>文件名仅用于显示，磁盘名称由平台生成</small>
          </label>
          <input
            className="visually-hidden"
            id="reconstruction-images"
            type="file"
            accept="image/jpeg,image/png,.jpg,.jpeg,.png"
            multiple
            disabled={reconstruction.isPending}
            onChange={selectFiles}
          />

          {files.length > 0 ? (
            <div className="selection-summary" aria-live="polite">
              <strong>{files.length} 张图片</strong>
              <span>合计 {formatBytes(totalBytes)}</span>
              <ul>
                {files.slice(0, 4).map((file) => (
                  <li key={`${file.name}-${file.size}-${file.lastModified}`}>
                    <span>{file.name}</span><small>{formatBytes(file.size)}</small>
                  </li>
                ))}
                {files.length > 4 ? <li>另有 {files.length - 4} 张图片</li> : null}
              </ul>
            </div>
          ) : null}

          {selectionError ? (
            <div className="form-notice error" role="alert">
              {selectionError}
            </div>
          ) : null}
          {reconstruction.isError ? (
            <div className="form-notice error" role="alert">
              {readableError(reconstruction.error)}
            </div>
          ) : null}

          {reconstruction.isPending ? (
            <div className="upload-progress" aria-live="polite">
              <div><span>上传并校验</span><strong>{uploadedCount}/{files.length}</strong></div>
              <progress max={100} value={progress}>{progress}%</progress>
            </div>
          ) : null}

          <button
            className="button primary"
            type="button"
            disabled={!canSubmit}
            onClick={() => reconstruction.mutate(files)}
          >
            {reconstruction.isPending ? "正在创建任务…" : "上传并创建模拟任务"}
          </button>
          <p className="privacy-note">图片可能包含 EXIF 位置信息；当前版本不会主动清除元数据，仅用于本机开发验证。</p>
        </article>

        <article className="workspace-card">
          <div className="card-label">PIPELINE</div>
          <h2>任务执行状态</h2>
          {displayedLatestJob ? (
            <div className="latest-job" role="status">
              <span className="status-pill">
                {statusLabels[displayedLatestJob.status] ?? displayedLatestJob.status}
              </span>
              <strong>{displayedLatestJob.job_id}</strong>
              <p>任务已进入数据库队列。启动独立模拟 Worker 后，页面会定期读取最新状态。</p>
            </div>
          ) : (
            <ol className="stage-list">
              {[
                "图片上传与解码校验",
                "不可变输入清单",
                "三分支模拟执行",
                "结构评估报告",
              ].map((stage, index) => (
                <li key={stage}><span>{String(index + 1).padStart(2, "0")}</span>{stage}</li>
              ))}
            </ol>
          )}
        </article>

        <article className="workspace-card wide">
          <div className="card-label">RECENT JOBS</div>
          <div className="jobs-heading">
            <h2>最近任务</h2>
            <button
              type="button"
              onClick={() => void jobs.refetch()}
              disabled={jobs.isFetching}
            >
              刷新
            </button>
          </div>
          {jobs.isError ? (
            <div className="form-notice error">{readableError(jobs.error)}</div>
          ) : jobs.data?.length ? (
            <div className="job-list">
              {jobs.data.map((job) => (
                <article key={job.job_id}>
                  <div>
                    <strong>{job.job_id}</strong>
                    <span>{new Date(job.created_at).toLocaleString("zh-CN")}</span>
                  </div>
                  <div className="job-status">
                    <span>{job.execution_mode === "simulated" ? "模拟" : "真实"}</span>
                    <strong>{statusLabels[job.status] ?? job.status}</strong>
                    <small>{job.progress}%</small>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <p className="empty-state">尚无任务。选择至少三张图片后即可建立第一条开发任务。</p>
          )}
        </article>
      </section>
    </main>
  );
}
