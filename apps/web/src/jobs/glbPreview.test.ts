import { describe, expect, it } from "vitest";

import type { ResultBranchSummary } from "./api";
import { selectGlbPreviewBranches } from "./glbPreview";

function branch(
  name: string,
  artifacts: ResultBranchSummary["artifacts"],
): ResultBranchSummary {
  return {
    name,
    status: "succeeded",
    duration_seconds: 10,
    artifacts,
    metrics: { vertices: 120, faces: 80 },
  };
}

describe("selectGlbPreviewBranches", () => {
  it("keeps only downloadable GLB artifacts in pipeline order", () => {
    const branches = [
      branch("A-v4", [
        {
          kind: "obj",
          size_bytes: 20,
          content_type: "text/plain",
          download_url: "/jobs/job-id/A-v4/obj",
        },
        {
          kind: "glb",
          size_bytes: 40,
          content_type: "model/gltf-binary",
          download_url: "/jobs/job-id/A-v4/glb",
        },
      ]),
      branch("B-v2", [{
        kind: "glb",
        size_bytes: 30,
        content_type: "model/gltf-binary",
        download_url: null,
      }]),
      branch("C", [{
        kind: "glb",
        size_bytes: 50,
        content_type: "model/gltf-binary",
        download_url: "/jobs/job-id/C/glb",
      }]),
    ];

    const result = selectGlbPreviewBranches(branches);

    expect(result.map((item) => item.name)).toEqual(["A-v4", "C"]);
    expect(result[0].artifact.kind).toBe("glb");
    expect(result[0].metrics).toEqual({ vertices: 120, faces: 80 });
  });
});
