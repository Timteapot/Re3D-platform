import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { JobTerminationNotice } from "./JobTerminationNotice";

describe("job termination reason", () => {
  it("explains low disk automatic cancellation", () => {
    render(<JobTerminationNotice status="cancelled" errorCode="LOW_DISK_AUTO_CANCELLED" />);
    expect(screen.getByRole("status")).toHaveTextContent("系统已自动取消任务");
    expect(screen.getByRole("status")).toHaveTextContent("数据盘空闲空间已达到安全线");
  });
  it("distinguishes a failed disk probe from user cancellation", () => {
    const { rerender } = render(<JobTerminationNotice status="cancelled" errorCode="DISK_GUARD_PROBE_FAILED" />);
    expect(screen.getByRole("status")).toHaveTextContent("无法可靠读取磁盘");
    rerender(<JobTerminationNotice status="cancelled" errorCode="USER_CANCELLED" />);
    expect(screen.getByRole("status")).toHaveTextContent("用户请求取消");
  });
  it("does not display private or unknown error strings", () => {
    const { rerender } = render(<JobTerminationNotice status="failed_pipeline" errorCode="private-path-or-error-body" />);
    expect(screen.getByRole("status")).toHaveTextContent("请根据任务记录");
    expect(screen.getByRole("status")).not.toHaveTextContent("private-path-or-error-body");
    rerender(<JobTerminationNotice status="failed_pipeline" errorCode="constructor" />);
    expect(screen.getByRole("status")).toHaveTextContent("请根据任务记录");
  });
  it("does not show a termination reason for an active or successful task", () => {
    const { rerender } = render(<JobTerminationNotice status="dense_reconstruction" errorCode="LOW_DISK_AUTO_CANCELLED" />);
    expect(screen.queryByRole("status")).toBeNull();
    rerender(<JobTerminationNotice status="succeeded" errorCode="LOW_DISK_AUTO_CANCELLED" />);
    expect(screen.queryByRole("status")).toBeNull();
  });
});
