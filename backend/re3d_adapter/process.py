from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .errors import AdapterError, PipelineCancelled, PipelineTimedOut


@dataclass(frozen=True)
class ManagedProcessOutcome:
    return_code: int
    duration_seconds: float


def run_managed_process(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: int,
    cancel_requested: Callable[[], bool] = lambda: False,
    health_check: Callable[[], None] = lambda: None,
    on_line: Callable[[str], None] = lambda _line: None,
    poll_seconds: float = 0.25,
    termination_grace_seconds: float = 5.0,
    env: Mapping[str, str] | None = None,
) -> ManagedProcessOutcome:
    """Run a command while supervising cancellation, timeout and process-tree exit."""
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be positive")
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    if termination_grace_seconds <= 0:
        raise ValueError("termination_grace_seconds must be positive")
    if not command:
        raise ValueError("command must not be empty")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    line_queue: queue.Queue[str] = queue.Queue()
    popen_options: dict[str, object] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            **popen_options,
        )
    except OSError as exc:
        raise AdapterError("cannot start the Re3D process") from exc

    assert process.stdout is not None

    def read_output() -> None:
        try:
            for line in process.stdout:
                line_queue.put(line)
        finally:
            process.stdout.close()

    reader = threading.Thread(
        target=read_output,
        name=f"re3d-output-{process.pid}",
        daemon=True,
    )
    reader.start()

    try:
        with log_path.open("a", encoding="utf-8", newline="\n") as log:
            log.write("\n--- managed Re3D process started ---\n")
            log.flush()
            while True:
                _drain_output(line_queue, log, on_line)
                health_check()
                if cancel_requested():
                    _terminate_process_tree(process, termination_grace_seconds)
                    raise PipelineCancelled("Re3D execution was cancelled")
                if time.monotonic() - started >= timeout_seconds:
                    _terminate_process_tree(process, termination_grace_seconds)
                    raise PipelineTimedOut("Re3D execution timed out")
                return_code = process.poll()
                if return_code is not None:
                    reader.join(timeout=max(1.0, termination_grace_seconds))
                    _drain_output(line_queue, log, on_line)
                    log.write(
                        f"--- managed Re3D process exited: {return_code} ---\n"
                    )
                    log.flush()
                    return ManagedProcessOutcome(
                        return_code=return_code,
                        duration_seconds=max(0.0, time.monotonic() - started),
                    )
                time.sleep(poll_seconds)
    except BaseException:
        if process.poll() is None:
            _terminate_process_tree(process, termination_grace_seconds)
        raise
    finally:
        reader.join(timeout=max(1.0, termination_grace_seconds))


def _drain_output(
    lines: queue.Queue[str],
    log,
    on_line: Callable[[str], None],
) -> None:
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            return
        log.write(line)
        log.flush()
        on_line(line.rstrip("\r\n"))


def _terminate_process_tree(
    process: subprocess.Popen[str],
    grace_seconds: float,
) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            completed = None
        if (
            (completed is None or completed.returncode != 0)
            and process.poll() is None
        ):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    process.wait(timeout=grace_seconds)
