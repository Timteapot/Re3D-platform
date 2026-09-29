import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

import { ApiError } from "../api/http";
import {
  fetchJobArtifact,
  type AuthorizedFetch,
  type ResultBranchSummary,
} from "./api";
import { selectGlbPreviewBranches } from "./glbPreview";
import {
  applyModelRotation,
  AUTO_MODEL_ROTATION,
  centerModelContent,
  defaultCameraDirection,
  prepareReconstructionModel,
  type ModelRotationDegrees,
} from "./modelPresentation";

interface GlbViewerProps {
  branches: ResultBranchSummary[];
  executionMode: "simulated" | "real";
  fetchAuthorized: AuthorizedFetch;
}

interface ModelStats {
  meshes: number;
  vertices: number;
  triangles: number;
}

type ViewerPhase = "idle" | "loading" | "ready" | "empty" | "error" | "unsupported";

interface ViewerState {
  phase: ViewerPhase;
  message: string;
  stats: ModelStats | null;
}

interface ViewerRuntime {
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  renderer: THREE.WebGLRenderer;
  controls: OrbitControls;
  grid: THREE.GridHelper;
  model: THREE.Object3D | null;
  cameraDirection: THREE.Vector3 | null;
  resetCamera: (() => void) | null;
}

const initialViewerState: ViewerState = {
  phase: "idle",
  message: "选择一个分支开始预览。",
  stats: null,
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function metric(metrics: Record<string, number | boolean>, key: string): string {
  const value = metrics[key];
  return typeof value === "number" ? value.toLocaleString("zh-CN") : "—";
}

function disposeMaterial(material: THREE.Material): void {
  for (const value of Object.values(material)) {
    if (value instanceof THREE.Texture) value.dispose();
  }
  material.dispose();
}

function disposeModel(root: THREE.Object3D): void {
  root.traverse((object) => {
    if (!(object instanceof THREE.Mesh)) return;
    object.geometry.dispose();
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of materials) disposeMaterial(material);
  });
}

function inspectModel(root: THREE.Object3D): ModelStats {
  const stats: ModelStats = { meshes: 0, vertices: 0, triangles: 0 };
  root.traverse((object) => {
    if (!(object instanceof THREE.Mesh)) return;
    stats.meshes += 1;
    const positions = object.geometry.getAttribute("position");
    if (!positions) return;
    stats.vertices += positions.count;
    stats.triangles += Math.floor(
      (object.geometry.index?.count ?? positions.count) / 3,
    );
  });
  return stats;
}

function clearModel(runtime: ViewerRuntime): void {
  if (runtime.model) {
    runtime.scene.remove(runtime.model);
    disposeModel(runtime.model);
  }
  runtime.model = null;
  runtime.cameraDirection = null;
  runtime.resetCamera = null;
  runtime.grid.visible = false;
}

function fitModel(
  runtime: ViewerRuntime,
  model: THREE.Object3D,
  cameraDirection: THREE.Vector3,
): THREE.Box3 | null {
  model.updateMatrixWorld(true);
  const bounds = new THREE.Box3().setFromObject(model, true);
  if (bounds.isEmpty()) return null;

  const size = bounds.getSize(new THREE.Vector3());
  const radius = Math.max(size.length() / 2, 0.001);

  const verticalFov = THREE.MathUtils.degToRad(runtime.camera.fov);
  const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * runtime.camera.aspect);
  const limitingFov = Math.max(Math.min(verticalFov, horizontalFov), 0.01);
  const distance = (radius / Math.sin(limitingFov / 2)) * 1.2;
  const fittedDirection = cameraDirection.clone().normalize();
  const resetCamera = () => {
    runtime.camera.position.copy(fittedDirection).multiplyScalar(distance);
    runtime.camera.near = Math.max(radius / 1000, 0.0001);
    runtime.camera.far = Math.max(distance + radius * 20, radius * 100);
    runtime.camera.updateProjectionMatrix();
    runtime.controls.target.set(0, 0, 0);
    runtime.controls.minDistance = Math.max(radius * 0.05, 0.0001);
    runtime.controls.maxDistance = distance * 20;
    runtime.controls.update();
  };

  runtime.cameraDirection = fittedDirection;
  runtime.resetCamera = resetCamera;
  resetCamera();
  runtime.grid.position.y = -size.y / 2;
  runtime.grid.scale.setScalar(Math.max(size.x, size.z, size.y) / 10 || 1);
  runtime.grid.visible = true;
  return bounds;
}

function readableViewerError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "GLB 模型加载失败。请确认产物完整，或改用下载功能检查文件。";
}

export default function GlbViewer({
  branches,
  executionMode,
  fetchAuthorized,
}: GlbViewerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const runtimeRef = useRef<ViewerRuntime | null>(null);
  const previewBranches = useMemo(
    () => selectGlbPreviewBranches(branches),
    [branches],
  );
  const [selectedName, setSelectedName] = useState("");
  const [viewerState, setViewerState] = useState<ViewerState>(initialViewerState);
  const [modelRotation, setModelRotation] = useState<ModelRotationDegrees>({
    ...AUTO_MODEL_ROTATION,
  });

  const selectedBranch = previewBranches.find((branch) => branch.name === selectedName);

  useEffect(() => {
    if (!previewBranches.some((branch) => branch.name === selectedName)) {
      setSelectedName(previewBranches[0]?.name ?? "");
    }
  }, [previewBranches, selectedName]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: false,
        powerPreference: "high-performance",
      });
    } catch {
      setViewerState({
        phase: "unsupported",
        message: "当前浏览器或图形环境无法创建 WebGL 查看器，仍可下载 GLB 文件。",
        stats: null,
      });
      return;
    }

    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.domElement.className = "glb-viewer-canvas";
    renderer.domElement.setAttribute("aria-label", "三维重建结果交互视图");
    renderer.domElement.setAttribute("role", "img");
    container.append(renderer.domElement);

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xe7ebe7);
    const camera = new THREE.PerspectiveCamera(42, 1, 0.01, 1000);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.075;
    controls.screenSpacePanning = true;

    const hemisphere = new THREE.HemisphereLight(0xffffff, 0x53665d, 2.1);
    const keyLight = new THREE.DirectionalLight(0xffffff, 2.4);
    keyLight.position.set(4, 7, 5);
    scene.add(hemisphere, keyLight);

    const grid = new THREE.GridHelper(10, 10, 0x809087, 0xb5beb8);
    grid.visible = false;
    scene.add(grid);

    const runtime: ViewerRuntime = {
      scene,
      camera,
      renderer,
      controls,
      grid,
      model: null,
      cameraDirection: null,
      resetCamera: null,
    };
    runtimeRef.current = runtime;

    const resize = () => {
      const width = Math.max(container.clientWidth, 1);
      const height = Math.max(container.clientHeight, 1);
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    resize();

    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(resize);
    resizeObserver?.observe(container);
    if (!resizeObserver) window.addEventListener("resize", resize);

    renderer.setAnimationLoop(() => {
      controls.update();
      renderer.render(scene, camera);
    });

    return () => {
      resizeObserver?.disconnect();
      if (!resizeObserver) window.removeEventListener("resize", resize);
      renderer.setAnimationLoop(null);
      clearModel(runtime);
      controls.dispose();
      grid.geometry.dispose();
      const gridMaterials = Array.isArray(grid.material) ? grid.material : [grid.material];
      for (const material of gridMaterials) material.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      renderer.domElement.remove();
      runtimeRef.current = null;
    };
  }, []);

  useEffect(() => {
    const runtime = runtimeRef.current;
    if (!runtime?.model) return;

    applyModelRotation(runtime.model, modelRotation);
    fitModel(
      runtime,
      runtime.model,
      runtime.cameraDirection ?? defaultCameraDirection(),
    );
  }, [modelRotation]);

  useEffect(() => {
    const runtime = runtimeRef.current;
    if (!runtime || !selectedBranch) return;

    const controller = new AbortController();
    let cancelled = false;
    clearModel(runtime);
    setModelRotation({ ...AUTO_MODEL_ROTATION });
    setViewerState({
      phase: "loading",
      message: `正在通过受鉴权接口加载 ${selectedBranch.name} 的 GLB…`,
      stats: null,
    });

    void (async () => {
      let objectUrl: string | null = null;
      try {
        const blob = await fetchJobArtifact(
          fetchAuthorized,
          selectedBranch.artifact,
          controller.signal,
        );
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        const gltf = await new GLTFLoader().loadAsync(objectUrl);
        if (cancelled) {
          disposeModel(gltf.scene);
          return;
        }

        const stats = inspectModel(gltf.scene);
        if (!centerModelContent(gltf.scene)) {
          setViewerState({
            phase: "empty",
            message: "该 GLB 已加载，但没有有效包围盒。",
            stats,
          });
          return;
        }

        const modelPivot = new THREE.Group();
        modelPivot.add(gltf.scene);
        applyModelRotation(modelPivot, AUTO_MODEL_ROTATION);
        runtime.scene.add(modelPivot);
        runtime.model = modelPivot;
        const cameraDirection = prepareReconstructionModel(modelPivot);
        if (!fitModel(runtime, modelPivot, cameraDirection) || stats.meshes === 0) {
          setViewerState({
            phase: "empty",
            message: executionMode === "simulated"
              ? "模拟任务的 GLB 是用于验证链路的空场景，不包含可渲染网格。"
              : "该 GLB 已加载，但没有可渲染的网格或有效包围盒。",
            stats,
          });
          return;
        }
        setViewerState({
          phase: "ready",
          message: `${selectedBranch.name} 已加载，可旋转、缩放和平移。`,
          stats,
        });
      } catch (error) {
        if (cancelled || (error instanceof DOMException && error.name === "AbortError")) return;
        setViewerState({
          phase: "error",
          message: readableViewerError(error),
          stats: null,
        });
      } finally {
        if (objectUrl) URL.revokeObjectURL(objectUrl);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [executionMode, fetchAuthorized, selectedBranch]);

  if (previewBranches.length === 0) {
    return <p className="empty-state">结果中没有可通过鉴权接口读取的 GLB 产物。</p>;
  }

  return (
    <div className="glb-viewer-shell">
      <div className="glb-viewer-toolbar">
        <div className="glb-branch-tabs" role="tablist" aria-label="重建分支">
          {previewBranches.map((branch) => (
            <button
              key={branch.name}
              type="button"
              role="tab"
              aria-selected={branch.name === selectedName}
              className={branch.name === selectedName ? "active" : ""}
              onClick={() => setSelectedName(branch.name)}
            >
              {branch.name}
            </button>
          ))}
        </div>
        <button
          className="glb-reset-button"
          type="button"
          disabled={viewerState.phase !== "ready"}
          onClick={() => runtimeRef.current?.resetCamera?.()}
        >
          重置视角
        </button>
      </div>

      <div className="glb-orientation-toolbar" aria-label="模型坐标旋转">
        <div>
          <strong>模型坐标角度</strong>
          <small>自动转换 Re3D 坐标；滑杆可绕各轴自由旋转。</small>
        </div>
        <div className="glb-rotation-controls">
          {(["x", "y", "z"] as const).map((axis) => (
            <label key={axis}>
              <span>{axis.toUpperCase()}</span>
              <input
                type="range"
                min="-180"
                max="180"
                step="1"
                value={modelRotation[axis]}
                disabled={viewerState.phase !== "ready"}
                aria-label={`模型 ${axis.toUpperCase()} 轴旋转角度`}
                onChange={(event) => {
                  const value = event.currentTarget.valueAsNumber;
                  setModelRotation((current) => ({
                    ...current,
                    [axis]: value,
                  }));
                }}
              />
              <output>{modelRotation[axis]}°</output>
            </label>
          ))}
        </div>
        <button
          className="glb-reset-button"
          type="button"
          disabled={viewerState.phase !== "ready"}
          onClick={() => setModelRotation({ ...AUTO_MODEL_ROTATION })}
        >
          恢复自动朝向
        </button>
      </div>

      <div
        ref={containerRef}
        className="glb-viewer-stage"
        aria-busy={viewerState.phase === "loading"}
      >
        {viewerState.phase !== "ready" ? (
          <div
            className={`glb-viewer-status ${viewerState.phase}`}
            role={viewerState.phase === "error" ? "alert" : "status"}
          >
            {viewerState.phase === "loading" ? <span className="spinner" /> : null}
            <span>{viewerState.message}</span>
          </div>
        ) : null}
      </div>

      {selectedBranch ? (
        <div className="glb-viewer-meta">
          <span>GLB {formatBytes(selectedBranch.artifact.size_bytes)}</span>
          <span>报告顶点 {metric(selectedBranch.metrics, "vertices")}</span>
          <span>报告面数 {metric(selectedBranch.metrics, "faces")}</span>
          {viewerState.stats ? <span>已加载网格 {viewerState.stats.meshes}</span> : null}
          <small>左键旋转视角 · 滚轮缩放 · 右键平移 · 滑杆旋转模型</small>
        </div>
      ) : null}
    </div>
  );
}
