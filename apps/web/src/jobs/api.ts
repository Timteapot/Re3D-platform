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
  submitted_at: string | null;
  images: UploadedImage[];
  reused: boolean;
}

export interface Job {
  job_id: string;
  status: string;
  execution_mode: "simulated" | "real";
  progress: number;
  created_at: string;
  queued_at: string;
}

export type AuthorizedRequest = <T>(
  path: string,
  init?: RequestInit,
) => Promise<T>;

export function createUpload(
  request: AuthorizedRequest,
  idempotencyKey: string,
): Promise<UploadSession> {
  return request<UploadSession>("/api/v1/uploads", {
    method: "POST",
    body: JSON.stringify({ idempotency_key: idempotencyKey }),
  });
}

export function uploadImage(
  request: AuthorizedRequest,
  uploadId: string,
  file: File,
): Promise<UploadSession> {
  const body = new FormData();
  body.append("file", file, file.name);
  return request<UploadSession>(`/api/v1/uploads/${uploadId}/images`, {
    method: "POST",
    body,
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
