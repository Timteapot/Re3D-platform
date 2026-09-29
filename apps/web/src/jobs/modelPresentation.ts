import * as THREE from "three";

const CAMERA_ELEVATION = 0.65;
const MIN_SURFACE_COHERENCE = 0.05;
const MIN_VECTOR_LENGTH = 1e-8;

export interface ModelRotationDegrees {
  x: number;
  y: number;
  z: number;
}

// Re3D follows the COLMAP/OpenCV camera frame: X right, Y down, Z forward.
// Rotating 180 degrees around X converts it to Three.js's Y-up convention.
export const AUTO_MODEL_ROTATION: Readonly<ModelRotationDegrees> = {
  x: 180,
  y: 0,
  z: 0,
};

export function defaultCameraDirection(): THREE.Vector3 {
  return new THREE.Vector3(1, CAMERA_ELEVATION, 1).normalize();
}

function prepareMaterial(material: THREE.Material): THREE.Material {
  if (material instanceof THREE.MeshStandardMaterial && material.map) {
    const previewMaterial = new THREE.MeshBasicMaterial({
      alphaMap: material.alphaMap,
      alphaTest: material.alphaTest,
      color: material.color,
      depthTest: material.depthTest,
      depthWrite: material.depthWrite,
      map: material.map,
      opacity: material.opacity,
      side: THREE.DoubleSide,
      toneMapped: false,
      transparent: material.transparent,
      vertexColors: material.vertexColors,
    });
    previewMaterial.name = material.name
      ? `${material.name}-photogrammetry-preview`
      : "photogrammetry-preview";
    material.dispose();
    return previewMaterial;
  }

  if (material instanceof THREE.MeshStandardMaterial) {
    // Re3D exports photographic diffuse textures. Some generated GLBs omit
    // metallicFactor, whose glTF default of 1 would otherwise render them as metal.
    material.metalness = 0;
    material.roughness = 1;
  }

  material.side = THREE.DoubleSide;
  material.needsUpdate = true;
  return material;
}

export function centerModelContent(root: THREE.Object3D): THREE.Box3 | null {
  root.updateMatrixWorld(true);
  const bounds = new THREE.Box3().setFromObject(root, true);
  if (bounds.isEmpty()) return null;

  const center = bounds.getCenter(new THREE.Vector3());
  root.position.sub(center);
  root.updateMatrixWorld(true);
  return new THREE.Box3().setFromObject(root, true);
}

export function applyModelRotation(
  root: THREE.Object3D,
  rotation: ModelRotationDegrees,
): void {
  root.rotation.set(
    THREE.MathUtils.degToRad(rotation.x),
    THREE.MathUtils.degToRad(rotation.y),
    THREE.MathUtils.degToRad(rotation.z),
    "XYZ",
  );
  root.updateMatrixWorld(true);
}

export function prepareReconstructionModel(root: THREE.Object3D): THREE.Vector3 {
  const summedNormal = new THREE.Vector3();
  let totalDoubleArea = 0;

  const a = new THREE.Vector3();
  const b = new THREE.Vector3();
  const c = new THREE.Vector3();
  const ab = new THREE.Vector3();
  const ac = new THREE.Vector3();
  const faceNormal = new THREE.Vector3();
  const preparedMaterials = new Map<THREE.Material, THREE.Material>();

  root.updateMatrixWorld(true);
  root.traverse((object) => {
    if (!(object instanceof THREE.Mesh)) return;

    const geometry = object.geometry;
    const positions = geometry.getAttribute("position");
    if (!positions) return;

    const prepareCachedMaterial = (material: THREE.Material) => {
      const cached = preparedMaterials.get(material);
      if (cached) return cached;
      const prepared = prepareMaterial(material);
      preparedMaterials.set(material, prepared);
      return prepared;
    };
    object.material = Array.isArray(object.material)
      ? object.material.map(prepareCachedMaterial)
      : prepareCachedMaterial(object.material);

    const indices = geometry.getIndex();
    const vertexCount = indices?.count ?? positions.count;
    const triangleCount = Math.floor(vertexCount / 3);

    for (let triangle = 0; triangle < triangleCount; triangle += 1) {
      const offset = triangle * 3;
      const indexA = indices ? indices.getX(offset) : offset;
      const indexB = indices ? indices.getX(offset + 1) : offset + 1;
      const indexC = indices ? indices.getX(offset + 2) : offset + 2;

      a.set(positions.getX(indexA), positions.getY(indexA), positions.getZ(indexA))
        .applyMatrix4(object.matrixWorld);
      b.set(positions.getX(indexB), positions.getY(indexB), positions.getZ(indexB))
        .applyMatrix4(object.matrixWorld);
      c.set(positions.getX(indexC), positions.getY(indexC), positions.getZ(indexC))
        .applyMatrix4(object.matrixWorld);

      ab.subVectors(b, a);
      ac.subVectors(c, a);
      faceNormal.crossVectors(ab, ac);
      const doubleArea = faceNormal.length();
      if (!Number.isFinite(doubleArea) || doubleArea <= MIN_VECTOR_LENGTH) continue;

      summedNormal.add(faceNormal);
      totalDoubleArea += doubleArea;
    }
  });

  const summedLength = summedNormal.length();
  const horizontalLength = Math.hypot(summedNormal.x, summedNormal.z);
  const coherence = totalDoubleArea > 0 ? summedLength / totalDoubleArea : 0;
  if (
    !Number.isFinite(coherence)
    || coherence < MIN_SURFACE_COHERENCE
    || horizontalLength <= MIN_VECTOR_LENGTH
  ) {
    return defaultCameraDirection();
  }

  return new THREE.Vector3(
    summedNormal.x / horizontalLength,
    CAMERA_ELEVATION,
    summedNormal.z / horizontalLength,
  ).normalize();
}
