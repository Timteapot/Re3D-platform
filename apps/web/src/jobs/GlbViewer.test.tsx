import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Group } from "three";

import type { AuthorizedFetch, ResultBranchSummary } from "./api";
import GlbViewer from "./GlbViewer";

const viewerMocks = vi.hoisted(() => ({
  loadAsync: vi.fn(),
  rendererConstructed: vi.fn(),
}));

vi.mock("three", async (importOriginal) => {
  const original = await importOriginal<typeof import("three")>();

  class FakeWebGLRenderer {
    readonly domElement = document.createElement("canvas");
    outputColorSpace = original.SRGBColorSpace;
    toneMapping = original.NoToneMapping;
    toneMappingExposure = 1;

    constructor() {
      viewerMocks.rendererConstructed();
    }

    setPixelRatio(): void {}
    setSize(): void {}
    setAnimationLoop(): void {}
    render(): void {}
    dispose(): void {}
    forceContextLoss(): void {}
  }

  return { ...original, WebGLRenderer: FakeWebGLRenderer };
});

vi.mock("three/examples/jsm/controls/OrbitControls.js", () => ({
  OrbitControls: class FakeOrbitControls {
    enableDamping = false;
    dampingFactor = 0;
    screenSpacePanning = false;
    minDistance = 0;
    maxDistance = 0;
    readonly target = { set: vi.fn() };

    update(): void {}
    dispose(): void {}
  },
}));

vi.mock("three/examples/jsm/loaders/GLTFLoader.js", () => ({
  GLTFLoader: class FakeGltfLoader {
    loadAsync = viewerMocks.loadAsync;
  },
}));

function branches(downloadUrl: string | null): ResultBranchSummary[] {
  return [{
    name: "A-v4",
    status: "succeeded",
    duration_seconds: 1,
    artifacts: [{
      kind: "glb",
      size_bytes: 1024,
      content_type: "model/gltf-binary",
      download_url: downloadUrl,
    }],
    metrics: { vertices: 3, faces: 1 },
  }];
}

describe("GlbViewer lifecycle", () => {
  beforeEach(() => {
    viewerMocks.loadAsync.mockReset();
    viewerMocks.loadAsync.mockResolvedValue({ scene: new Group() });
    viewerMocks.rendererConstructed.mockReset();
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:re3d-test"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
  });

  it("initializes once when a preview becomes available after the first render", async () => {
    const fetchAuthorized = vi.fn(
      async () => new Response(new Blob(["glTF"])),
    ) as unknown as AuthorizedFetch;
    const { container, rerender } = render(
      <GlbViewer
        branches={branches(null)}
        executionMode="real"
        fetchAuthorized={fetchAuthorized}
      />,
    );

    expect(screen.getByText("结果中没有可通过鉴权接口读取的 GLB 产物。"))
      .toBeInTheDocument();
    expect(container.querySelector("canvas")).not.toBeInTheDocument();
    expect(viewerMocks.rendererConstructed).not.toHaveBeenCalled();

    rerender(
      <GlbViewer
        branches={branches("/api/v1/jobs/job-id/artifacts/A-v4/glb")}
        executionMode="real"
        fetchAuthorized={fetchAuthorized}
      />,
    );

    await waitFor(() => {
      expect(container.querySelector("canvas.glb-viewer-canvas")).toBeInTheDocument();
      expect(fetchAuthorized).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByRole("img", { name: "三维重建结果交互视图" }))
      .toBeInTheDocument();
    await waitFor(() => {
      expect(viewerMocks.loadAsync).toHaveBeenCalledWith("blob:re3d-test");
    });
    expect(await screen.findByText("该 GLB 已加载，但没有有效包围盒。"))
      .toBeInTheDocument();
    expect(fetchAuthorized).toHaveBeenCalledWith(
      "/api/v1/jobs/job-id/artifacts/A-v4/glb",
      expect.objectContaining({
        headers: { Accept: "model/gltf-binary" },
        signal: expect.any(AbortSignal),
      }),
    );
    expect(viewerMocks.rendererConstructed).toHaveBeenCalledTimes(1);

    rerender(
      <GlbViewer
        branches={branches("/api/v1/jobs/job-id/artifacts/A-v4/glb")}
        executionMode="real"
        fetchAuthorized={fetchAuthorized}
      />,
    );

    expect(viewerMocks.rendererConstructed).toHaveBeenCalledTimes(1);
    expect(container.querySelectorAll("canvas.glb-viewer-canvas")).toHaveLength(1);
  });
});
