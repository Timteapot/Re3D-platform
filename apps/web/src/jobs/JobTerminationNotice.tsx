interface JobTerminationNoticeProps {
  status: string;
  errorCode: string | null;
}

const messages: Record<string, string> = {
  LOW_DISK_AUTO_CANCELLED: "数据盘空闲空间已达到安全线，系统已自动取消任务以保护存储。",
  DISK_GUARD_PROBE_FAILED: "系统无法可靠读取磁盘空闲空间，已自动取消任务以保护存储。",
  USER_CANCELLED: "任务已按用户请求取消。",
  REAL_PIPELINE_TIMEOUT: "重建超过执行时间预算，已停止运行。",
};

export function JobTerminationNotice({ status, errorCode }: JobTerminationNoticeProps) {
  if (!errorCode || !["cancelled", "failed_input", "failed_pipeline", "failed_evaluation"].includes(status)) {
    return null;
  }
  const message = Object.hasOwn(messages, errorCode)
    ? messages[errorCode]
    : "任务未完成，请根据任务记录检查输入或联系运维。";
  return (
    <div className="form-notice error" role="status">
      <strong>任务停止原因</strong>
      <p>{message}</p>
    </div>
  );
}
