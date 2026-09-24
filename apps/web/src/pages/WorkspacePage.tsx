import { useMemo, useRef, useState, type ChangeEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { ApiError } from "../api/http";
import { useAuth } from "../auth/AuthContext";
import {
  cancelUpload,
  createUpload,
  deleteUploadedImage,
  listJobs,
  submitUpload,
  uploadImage,
  type Job,
  type UploadSession,
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
  if (error instanceof DOMException && error.name === "AbortError") {
    return "上传请求已停止，正在取消上传会话。";
  }
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
  const draftKey = useRef(crypto.randomUUID());
  const uploadAbort = useRef<AbortController | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [operationNotice, setOperationNotice] = useState<string | null>(null);
  const [uploadedCount, setUploadedCount] = useState(0);
  const [batchTotal, setBatchTotal] = useState(0);
  const [uploadSession, setUploadSession] = useState<UploadSession | null>(null);
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

  const uploadFiles = useMutation({
    mutationFn: async (selected: File[]) => {
      const controller = new AbortController();
      uploadAbort.current = controller;
      setUploadedCount(0);
      setBatchTotal(selected.length);
      setOperationNotice(null);

      let session = uploadSession;
      if (session === null) {
        session = await createUpload(
          auth.request,
          draftKey.current,
          controller.signal,
        );
        setUploadSession(session);
      }
      for (const [index, file] of selected.entries()) {
        session = await uploadImage(
          auth.request,
          session.upload_id,
          file,
          controller.signal,
        );
        setUploadSession(session);
        setUploadedCount(index + 1);
        setFiles(selected.slice(index + 1));
      }
      return session;
    },
    onSuccess: (session) => {
      setUploadSession(session);
      setFiles([]);
      setOperationNotice(
        `已验证 ${session.image_count} 张服务端图片，可继续添加、删除或提交。`,
      );
    },
    onSettled: () => {
      uploadAbort.current = null;
      setBatchTotal(0);
      setUploadedCount(0);
    },
  });

  const deleteImage = useMutation({
    mutationFn: (imageId: string) => {
      if (uploadSession === null) throw new Error("upload session is missing");
      return deleteUploadedImage(
        auth.request,
        uploadSession.upload_id,
        imageId,
      );
    },
    onSuccess: (session) => {
      setUploadSession(session);
      setOperationNotice("图片已从未提交上传中删除。");
    },
  });

  const submitDraft = useMutation({
    mutationFn: (uploadId: string) => submitUpload(auth.request, uploadId),
    onSuccess: (job) => {
      setLatestJob(job);
      setUploadSession(null);
      setFiles([]);
      draftKey.current = crypto.randomUUID();
      setOperationNotice("输入已冻结，任务已进入模拟队列。");
      void queryClient.invalidateQueries({ queryKey: ["jobs", auth.user?.id] });
    },
  });

  const cancelDraft = useMutation({
    mutationFn: async (uploadId: string) => {
      uploadAbort.current?.abort();
      return cancelUpload(auth.request, uploadId);
    },
    onSuccess: (cancelled) => {
      setUploadSession(null);
      setFiles([]);
      setBatchTotal(0);
      setUploadedCount(0);
      draftKey.current = crypto.randomUUID();
      uploadFiles.reset();
      deleteImage.reset();
      submitDraft.reset();
      setOperationNotice(
        cancelled.storage_removed
          ? "上传已取消，未提交图片目录已经删除。"
          : "上传已取消，目录将由维护任务再次清理。",
      );
    },
  });

  function selectFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []);
    uploadFiles.reset();
    setUploadedCount(0);
    setOperationNotice(null);
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
    const existingBytes = uploadSession?.total_bytes ?? 0;
    if (existingBytes + selectedBytes > MAX_TOTAL_BYTES) {
      setFiles([]);
      setSelectionError("当前会话与所选图片合计超过 1 GiB 的开发限制。");
      event.target.value = "";
      return;
    }
    const existingCount = uploadSession?.image_count ?? 0;
    if (existingCount + selected.length > MAX_IMAGES) {
      setFiles([]);
      setSelectionError(`当前上传会话累计不能超过 ${MAX_IMAGES} 张图片。`);
      event.target.value = "";
      return;
    }
    setFiles(selected);
    setSelectionError(null);
    event.target.value = "";
  }

  const operationError =
    uploadFiles.error ?? deleteImage.error ?? submitDraft.error ?? cancelDraft.error;
  const busy =
    uploadFiles.isPending ||
    deleteImage.isPending ||
    submitDraft.isPending ||
    cancelDraft.isPending;
  const canUpload = files.length > 0 && !busy;
  const canSubmit =
    uploadSession !== null && uploadSession.image_count >= 3 && !busy;
  const progress =
    batchTotal > 0 ? Math.round((uploadedCount / batchTotal) * 100) : 0;
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
          <h2>管理多视图输入</h2>
          <p>支持 JPEG、PNG，3–150 张，单张最大 25 MB、合计最大 1 GiB。图片先验证并保留在可修改上传中，确认后再冻结并入队。</p>
          <label className="file-picker" htmlFor="reconstruction-images">
            <span>选择一批图片</span>
            <small>可分批添加；磁盘名称由平台生成</small>
          </label>
          <input
            className="visually-hidden"
            id="reconstruction-images"
            type="file"
            accept="image/jpeg,image/png,.jpg,.jpeg,.png"
            multiple
            disabled={busy}
            onChange={selectFiles}
          />

          {files.length > 0 ? (
            <div className="selection-summary" aria-live="polite">
              <strong>待上传 {files.length} 张</strong>
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

          {uploadSession ? (
            <div className="server-upload" aria-live="polite">
              <div>
                <strong>服务端已验证 {uploadSession.image_count} 张</strong>
                <span>{formatBytes(uploadSession.total_bytes)}</span>
              </div>
              {uploadSession.images.length ? (
                <ul>
                  {uploadSession.images.map((image) => (
                    <li key={image.id}>
                      <span>
                        <strong>{image.original_name}</strong>
                        <small>
                          {image.width}×{image.height} · {formatBytes(image.size_bytes)}
                        </small>
                      </span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => deleteImage.mutate(image.id)}
                      >
                        删除
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p>会话已创建，尚未保存图片。</p>
              )}
            </div>
          ) : null}

          {selectionError ? (
            <div className="form-notice error" role="alert">
              {selectionError}
            </div>
          ) : null}
          {operationError ? (
            <div className="form-notice error" role="alert">
              {readableError(operationError)}
            </div>
          ) : null}
          {operationNotice ? (
            <div className="form-notice success" role="status">
              {operationNotice}
            </div>
          ) : null}

          {uploadFiles.isPending ? (
            <div className="upload-progress" aria-live="polite">
              <div><span>上传并校验</span><strong>{uploadedCount}/{batchTotal}</strong></div>
              <progress max={100} value={progress}>{progress}%</progress>
            </div>
          ) : null}

          <div className="upload-actions">
            <button
              className="button secondary"
              type="button"
              disabled={!canUpload}
              onClick={() => uploadFiles.mutate(files)}
            >
              {uploadFiles.isPending ? "正在上传…" : "上传所选图片"}
            </button>
            <button
              className="button primary"
              type="button"
              disabled={!canSubmit}
              onClick={() => {
                if (uploadSession) submitDraft.mutate(uploadSession.upload_id);
              }}
            >
              {submitDraft.isPending ? "正在提交…" : "冻结输入并创建模拟任务"}
            </button>
            {uploadSession ? (
              <button
                className="button danger"
                type="button"
                disabled={
                  cancelDraft.isPending ||
                  deleteImage.isPending ||
                  submitDraft.isPending
                }
                onClick={() => {
                  const confirmed = window.confirm(
                    "取消后将删除本次尚未提交的全部图片，是否继续？",
                  );
                  if (confirmed) cancelDraft.mutate(uploadSession.upload_id);
                }}
              >
                {cancelDraft.isPending ? "正在取消…" : "取消并删除未提交图片"}
              </button>
            ) : null}
          </div>
          {uploadSession && uploadSession.image_count < 3 ? (
            <p className="upload-hint">还需至少 {3 - uploadSession.image_count} 张有效图片才能提交。</p>
          ) : null}
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
              <Link className="text-link" to={`/workspace/jobs/${displayedLatestJob.job_id}`}>查看任务详情</Link>
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
                    <Link to={`/workspace/jobs/${job.job_id}`}>查看详情</Link>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <p className="empty-state">尚无任务。验证至少三张图片后即可建立第一条开发任务。</p>
          )}
        </article>
      </section>
    </main>
  );
}
