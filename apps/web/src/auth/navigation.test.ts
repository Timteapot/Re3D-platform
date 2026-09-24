import { describe, expect, it } from "vitest";

import { safeReturnPath } from "./navigation";

describe("safeReturnPath", () => {
  it("keeps internal paths", () => {
    expect(safeReturnPath("/workspace?job=123")).toBe("/workspace?job=123");
  });

  it("rejects external, protocol-relative, and missing paths", () => {
    expect(safeReturnPath("https://example.com")).toBe("/workspace");
    expect(safeReturnPath("//example.com")).toBe("/workspace");
    expect(safeReturnPath("/\\example.com")).toBe("/workspace");
    expect(safeReturnPath(null)).toBe("/workspace");
  });
});
