export interface UploadedImage {
  id: string;
  original_name: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  width: number;
  height: number;
}

export interface UploadSession {
  upload_id: string;
  status: "uploading" | "submitted" | "cancelled";
  image_count: number;
  total_bytes: number;
  created_at: string;
  updated_at: string;
  submitted_at: string | null;
  cancelled_at: string | null;
  cancellation_reason: "user" | "expired" | null;
  storage_cleaned_at: string | null;
  images: UploadedImage[];
  reused: boolean;
}

export interface UploadCancellation extends UploadSession {
  storage_removed: boolean;
}

export interface Job {
  job_id: string;
  user_id: string;
  status: string;
  execution_mode: "simulated" | "real";
  attempt: number;
  progress: number;
  cancel_requested: boolean;
  error_code: string | null;
  created_at: string;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  version: number;
  reused: boolean;
}

export interface JobInputSummary {
  image_count: number;
  total_bytes: number;
  branches: string[];
}

export interface ArtifactSummary {
  kind: string;
  size_bytes: number;
  content_type: string;
}

export interface ResultBranchSummary {
  name: string;
  status: string;
  duration_seconds: number;
  artifacts: ArtifactSummary[];
  metrics: Record<string, number | boolean>;
}

export interface ResultSummary {
  status: string;
  execution_mode: "simulated" | "real";
  duration_seconds: number;
  branches: ResultBranchSummary[];
}

export interface EvaluationBranchSummary {
  name: string;
  status: string;
  score: number | null;
  summary: string;
  artifact_status: string | null;
}

export interface EvaluationSummary {
  scope: string;
  rules_version: string;
  overall_status: string;
  score: number | null;
  summary: string;
  branches: EvaluationBranchSummary[];
  limitations: string[];
}

export interface JobDetail {
  job: Job;
  detail_state: "pending" | "available" | "not_available" | "missing" | "invalid";
  warning_code: string | null;
  input: JobInputSummary | null;
  result: ResultSummary | null;
  evaluation: EvaluationSummary | null;
}

export type AuthorizedRequest = <T>(
  path: string,
  init?: RequestInit,
) => Promise<T>;

export function createUpload(
  request: AuthorizedRequest,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<UploadSession> {
  return request<UploadSession>("/api/v1/uploads", {
    method: "POST",
    body: JSON.stringify({ idempotency_key: idempotencyKey }),
    ...(signal ? { signal } : {}),
  });
}

export function uploadImage(
  request: AuthorizedRequest,
  uploadId: string,
  file: File,
  signal?: AbortSignal,
): Promise<UploadSession> {
  const body = new FormData();
  body.append("file", file, file.name);
  return request<UploadSession>(`/api/v1/uploads/${uploadId}/images`, {
    method: "POST",
    body,
    ...(signal ? { signal } : {}),
  });
}

export function deleteUploadedImage(
  request: AuthorizedRequest,
  uploadId: string,
  imageId: string,
): Promise<UploadSession> {
  return request<UploadSession>(
    `/api/v1/uploads/${uploadId}/images/${imageId}`,
    { method: "DELETE" },
  );
}

export function cancelUpload(
  request: AuthorizedRequest,
  uploadId: string,
): Promise<UploadCancellation> {
  return request<UploadCancellation>(`/api/v1/uploads/${uploadId}/cancel`, {
    method: "POST",
  });
}

export function submitUpload(
  request: AuthorizedRequest,
  uploadId: string,
): Promise<Job> {
  return request<Job>(`/api/v1/uploads/${uploadId}/submit`, {
    method: "POST",
  });
}

export function listJobs(request: AuthorizedRequest): Promise<Job[]> {
  return request<Job[]>("/api/v1/development/jobs?limit=20");
}

export function getJobDetail(
  request: AuthorizedRequest,
  jobId: string,
): Promise<JobDetail> {
  return request<JobDetail>(`/api/v1/development/jobs/${jobId}/detail`);
}

export function cancelJob(
  request: AuthorizedRequest,
  jobId: string,
): Promise<Job> {
  return request<Job>(`/api/v1/development/jobs/${jobId}/cancel`, {
    method: "POST",
  });
}
