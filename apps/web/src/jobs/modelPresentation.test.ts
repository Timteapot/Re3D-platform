import { describe, expect, it } from "vitest";
import * as THREE from "three";

import {
  applyModelRotation,
  AUTO_MODEL_ROTATION,
  centerModelContent,
  defaultCameraDirection,
  prepareReconstructionModel,
} from "./modelPresentation";

describe("prepareReconstructionModel", () => {
  it("normalizes photogrammetry materials and faces the dominant surface", () => {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute(
      "position",
      new THREE.Float32BufferAttribute([
        -1, 0, 0,
        0, 1, 0,
        1, 0, 0,
      ], 3),
    );
    const texture = new THREE.Texture();
    const material = new THREE.MeshStandardMaterial({
      map: texture,
      metalness: 1,
      roughness: 0.9,
      side: THREE.FrontSide,
    });
    const model = new THREE.Group();
    model.add(new THREE.Mesh(geometry, material));

    const direction = prepareReconstructionModel(model);

    const preparedMaterial = (model.children[0] as THREE.Mesh)
      .material as THREE.MeshBasicMaterial;
    expect(preparedMaterial).toBeInstanceOf(THREE.MeshBasicMaterial);
    expect(preparedMaterial.map).toBe(texture);
    expect(preparedMaterial.side).toBe(THREE.DoubleSide);
    expect(direction.y).toBeGreaterThan(0);
    expect(direction.z).toBeLessThan(-0.8);
  });

  it("uses the stable default direction for a directionally balanced mesh", () => {
    const model = new THREE.Mesh(
      new THREE.BoxGeometry(1, 1, 1),
      new THREE.MeshStandardMaterial(),
    );

    const direction = prepareReconstructionModel(model);
    const fallback = defaultCameraDirection();

    expect(direction.x).toBeCloseTo(fallback.x);
    expect(direction.y).toBeCloseTo(fallback.y);
    expect(direction.z).toBeCloseTo(fallback.z);
  });

  it("centers the model and converts the Re3D frame to Y-up", () => {
    const geometry = new THREE.BoxGeometry(2, 4, 6);
    geometry.translate(10, -3, 7);
    const model = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial());

    const centeredBounds = centerModelContent(model);
    const centered = centeredBounds?.getCenter(new THREE.Vector3());
    expect(centered?.length()).toBeCloseTo(0);

    applyModelRotation(model, AUTO_MODEL_ROTATION);
    const downInRe3d = new THREE.Vector3(0, 1, 0).applyQuaternion(model.quaternion);
    expect(downInRe3d.y).toBeCloseTo(-1);
  });
});
