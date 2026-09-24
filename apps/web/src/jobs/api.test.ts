import { describe, expect, it, vi } from "vitest";

import { createUpload, submitUpload, uploadImage, type UploadSession } from "./api";

const upload: UploadSession = {
  upload_id: "2d37c9a2-c086-4c8c-af03-e08ce9fda255",
  status: "uploading",
  image_count: 0,
  total_bytes: 0,
  created_at: "2026-09-24T00:00:00Z",
  submitted_at: null,
  images: [],
  reused: false,
};

describe("job upload API", () => {
  it("creates an idempotent upload session", async () => {
    const request = vi.fn().mockResolvedValue(upload);
    await createUpload(request, "browser-request-001");

    expect(request).toHaveBeenCalledWith("/api/v1/uploads", {
      method: "POST",
      body: JSON.stringify({ idempotency_key: "browser-request-001" }),
    });
  });

  it("sends an image as multipart form data", async () => {
    const request = vi.fn().mockResolvedValue(upload);
    const file = new File([new Uint8Array([1, 2, 3])], "camera.png", {
      type: "image/png",
    });
    await uploadImage(request, upload.upload_id, file);

    const [, init] = request.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const attached = (init.body as FormData).get("file");
    expect(attached).toBeInstanceOf(File);
    expect((attached as File).name).toBe("camera.png");
    expect((attached as File).size).toBe(file.size);
  });

  it("submits only the server-side upload identifier", async () => {
    const request = vi.fn().mockResolvedValue({ status: "queued" });
    await submitUpload(request, upload.upload_id);

    expect(request).toHaveBeenCalledWith(
      `/api/v1/uploads/${upload.upload_id}/submit`,
      { method: "POST" },
    );
  });
});
