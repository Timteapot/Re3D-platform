"""Process containment for opt-in experiments, not the production adapter.

Windows starts the primary thread suspended, assigns its process to a private
KILL_ON_JOB_CLOSE Job Object, then resumes that thread. No breakaway permission
or inheritable job handle is granted. See Microsoft's Job Objects,
CreateProcessW, ResumeThread and PROC_THREAD_ATTRIBUTE_HANDLE_LIST documentation.
Only normal CreateProcess descendants are contained (not remote services/WMI).
POSIX uses a fresh session/process group; trusted tools must not escape it.
"""

from __future__ import annotations

import codecs
import math
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from backend.re3d_adapter.errors import AdapterError, PipelineTimedOut

_CHUNK_BYTES = 64 * 1024
_QUEUE_CHUNKS = 32
_LINE_CHARS = 64 * 1024


class ContainedProcessError(AdapterError):
    """A stable, non-secret supervisor failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ContainedProcessOutcome:
    return_code: int
    duration_seconds: float
    pid: int
    tree_stopped_confirmed: bool = True


def _open_binary_log(path: Path):
    """Only the asynchronous writer calls this or owns the resulting handle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("ab", buffering=0)


class _AsyncLogWriter:
    """A bounded queue isolates compute protection from blocked disk I/O.

    This thread alone opens/writes/flushes/closes its file. The owner must never
    close a descriptor concurrently with a blocked write. A kernel I/O call
    cannot be preempted by Python: on abandonment this daemon remains private,
    and closes its own handle if the call eventually returns.
    """

    def __init__(self, path: Path, stale_seconds: float):
        self.path = path
        self.stale_seconds = stale_seconds
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=_QUEUE_CHUNKS)
        self.ready = threading.Event()
        self.done = threading.Event()
        self.abort = threading.Event()
        self.finish_requested = threading.Event()
        self.failed = threading.Event()
        self._lock = threading.Lock()
        self._busy_since: float | None = time.monotonic()
        self.thread = threading.Thread(target=self._run, name="experiment-private-log", daemon=True)

    def _busy(self, value: bool):
        with self._lock:
            self._busy_since = time.monotonic() if value else None

    def _run(self):
        stream = None
        try:
            stream = _open_binary_log(self.path)
            self._busy(False)
            self.ready.set()
            while not self.abort.is_set():
                if self.finish_requested.is_set() and self.queue.empty():
                    break
                try:
                    chunk = self.queue.get(timeout=.05)
                except queue.Empty:
                    continue
                self._busy(True)
                remaining = memoryview(chunk)
                while remaining and not self.abort.is_set():
                    written = stream.write(remaining)
                    if type(written) is not int or written <= 0 or written > len(remaining):
                        raise OSError("invalid private log write")
                    remaining = remaining[written:]
                if not self.abort.is_set():
                    stream.flush()
                self._busy(False)
        except BaseException:
            self.failed.set()
        finally:
            # Neither this exception nor a close failure can expose the original
            # command, environment, filesystem path or disk error details.
            if stream is not None:
                self._busy(True)
                try:
                    stream.close()
                except BaseException:
                    self.failed.set()
            self._busy(False)
            self.done.set()
            self.ready.set()

    def start(self):
        self.thread.start()

    def check_health(self, *, finishing=False):
        if self.failed.is_set():
            raise ContainedProcessError("EXPERIMENT_PROCESS_IO_FAILED")
        with self._lock:
            busy_since = self._busy_since
        if busy_since is not None and time.monotonic() - busy_since >= self.stale_seconds:
            self.abort.set()
            raise ContainedProcessError("EXPERIMENT_PROCESS_LOG_IO_TIMED_OUT")
        if self.done.is_set() and not finishing:
            raise ContainedProcessError("EXPERIMENT_PROCESS_LOG_WRITER_STOPPED")
        if not self.thread.is_alive() and not self.done.is_set():
            raise ContainedProcessError("EXPERIMENT_PROCESS_LOG_WRITER_STOPPED")

    def submit(self, chunk: bytes) -> bool:
        try:
            self.queue.put_nowait(chunk)
            return True
        except queue.Full:
            return False

    def abandon(self):
        self.abort.set()
        # No fd close here. A stuck writer cannot delay Job Object teardown.
        if self.thread.ident is not None:
            self.thread.join(timeout=.25)


def _callback(function: Callable, *args) -> None:
    try:
        function(*args)
    except (AdapterError, KeyboardInterrupt):
        raise
    except BaseException:
        raise ContainedProcessError("EXPERIMENT_PROCESS_CALLBACK_FAILED") from None


def run_contained_process(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    health_check: Callable[[], None] = lambda: None,
    on_line: Callable[[str], None] = lambda _: None,
    env: Mapping[str, str] | None = None,
    poll_seconds: float = 0.25,
    termination_grace_seconds: float = 5.0,
    on_started: Callable[[int], None] = lambda _: None,
    on_tree_stopped: Callable[[], None] = lambda: None,
    log_io_timeout_seconds: float = 5.0,
) -> ContainedProcessOutcome:
    """Supervise one private tree; even a zero exit must pass final health.

    Raw output goes only to ``log_path`` (the caller must protect its ACL).
    Each read/write queue is bounded to 2 MiB. Newline-free output is emitted to on_line
    in at most 64 Ki-character fragments. Callback consumers must whitelist
    public fields; callbacks are never printed here. No command/env is logged.
    A parent exit closes its remaining descendants before returning success.
    on_tree_stopped is positive native teardown proof, never inferred from an
    exception's code. Consumers must keep all other callbacks nonblocking.
    """
    if isinstance(command, (str, bytes)) or not command or any(
        not isinstance(item, str) or "\0" in item for item in command
    ):
        raise ValueError("command must be a nonempty sequence of strings")
    for value in (timeout_seconds, poll_seconds, termination_grace_seconds, log_io_timeout_seconds):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("supervision durations must be finite and positive")
    started = time.monotonic()
    output: queue.Queue[bytes] = queue.Queue(maxsize=_QUEUE_CHUNKS)
    reader_done = threading.Event()
    reader_stop = threading.Event()
    reader_failed = threading.Event()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    process = None
    reader = None
    writer = _AsyncLogWriter(Path(log_path), log_io_timeout_seconds)
    held_chunk: bytes | None = None
    creation_attempted = False
    tree_stopped_confirmed = False
    teardown_attempted = False

    def confirm_tree_stopped():
        nonlocal tree_stopped_confirmed
        if not tree_stopped_confirmed:
            tree_stopped_confirmed = True
            _callback(on_tree_stopped)

    def stop_tree():
        nonlocal teardown_attempted
        # An interrupted/failed native stop is not retried here as though the
        # second attempt could establish retroactive proof after a second Ctrl+C.
        teardown_attempted = True
        process.stop(termination_grace_seconds)
        confirm_tree_stopped()

    def supervise(*, finishing_log=False) -> None:
        _callback(health_check)
        if time.monotonic() - started >= timeout_seconds:
            raise PipelineTimedOut("EXPERIMENT_PROCESS_TIMED_OUT")
        if reader_failed.is_set():
            raise ContainedProcessError("EXPERIMENT_PROCESS_IO_FAILED")
        writer.check_health(finishing=finishing_log)

    def read_output() -> None:
        try:
            while not reader_stop.is_set():
                chunk = process.stdout.read(_CHUNK_BYTES)
                if not chunk:
                    break
                while not reader_stop.is_set():
                    try:
                        output.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        pass
        except (OSError, ValueError):
            if not reader_stop.is_set():
                reader_failed.set()
        finally:
            process.stdout.close()
            reader_done.set()

    def deliver(text: str, *, final: bool = False) -> None:
        nonlocal pending
        pending += text
        while pending:
            newline = pending.find("\n", 0, _LINE_CHARS)
            if newline >= 0:
                line, pending = pending[:newline].rstrip("\r"), pending[newline + 1:]
            elif len(pending) >= _LINE_CHARS:
                line, pending = pending[:_LINE_CHARS], pending[_LINE_CHARS:]
            elif final:
                line, pending = pending.rstrip("\r"), ""
            else:
                break
            supervise()
            _callback(on_line, line)

    def drain() -> None:
        nonlocal held_chunk
        # Never drain until empty: a hot producer must not starve supervision.
        for _ in range(4):
            supervise()
            if held_chunk is None:
                try:
                    held_chunk = output.get_nowait()
                except queue.Empty:
                    break
            if not writer.submit(held_chunk):
                break
            chunk, held_chunk = held_chunk, None
            deliver(decoder.decode(chunk))

    try:
        writer.start()
        while not writer.ready.is_set():
            supervise()
            writer.ready.wait(min(poll_seconds, .05))
        supervise()
        try:
            creation_attempted = True
            process = (
                _WindowsProcess(command, Path(cwd), env)
                if os.name == "nt"
                else _PosixProcess(command, Path(cwd), env)
            )
        except ContainedProcessError:
            raise
        except (OSError, ValueError):
            raise ContainedProcessError("EXPERIMENT_PROCESS_START_FAILED") from None
        _callback(on_started, process.pid)
        supervise()
        process.start()
        reader = threading.Thread(
            target=read_output, name=f"experiment-output-{process.pid}", daemon=True
        )
        reader.start()
        while True:
            drain()
            supervise()
            return_code = process.poll()
            if return_code is not None:
                # Do not let a successful parent orphan still-running tools.
                stop_tree()
                while not reader_done.is_set() or not output.empty() or held_chunk is not None:
                    drain()
                    supervise()
                    reader_done.wait(min(poll_seconds, 0.05))
                deliver(decoder.decode(b"", final=True), final=True)
                supervise()
                writer.finish_requested.set()
                while not writer.done.is_set():
                    supervise(finishing_log=True)
                    writer.done.wait(min(poll_seconds, .05))
                supervise(finishing_log=True)
                return ContainedProcessOutcome(
                    return_code, max(0.0, time.monotonic() - started), process.pid,
                    tree_stopped_confirmed=tree_stopped_confirmed,
                )
            time.sleep(poll_seconds)
    except BaseException:
        if process is not None and not teardown_attempted:
            stop_tree()
        elif process is None and not creation_attempted:
            confirm_tree_stopped()  # No attempt to create a compute process.
        raise
    finally:
        reader_stop.set()
        try:
            if reader is not None:
                reader.join(timeout=termination_grace_seconds)
        finally:
            # A second Ctrl+C while joining the pipe reader must still close the
            # private Job fail-safe, but cannot fabricate completed native waits.
            try:
                if process is not None:
                    process.close()
            finally:
                writer.abandon()


class _PosixProcess:
    def __init__(self, command, cwd, env):
        self.process = subprocess.Popen(
            list(command), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
            start_new_session=True, close_fds=True,
        )
        self.pid = self.process.pid
        self.stdout = self.process.stdout

    def start(self):
        pass

    def poll(self):
        return self.process.poll()

    def _group_alive(self):
        # Linux zombies retain a PGID but cannot execute; ignore only zombies.
        proc = Path("/proc")
        if proc.is_dir():
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(fields[2]) == self.pid and fields[0] not in ("Z", "X"):
                        return True
                except (OSError, ValueError, IndexError):
                    continue
            return False
        try:
            os.killpg(self.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def stop(self, grace):
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + grace
        while self._group_alive() and time.monotonic() < deadline:
            self.process.poll()
            time.sleep(0.02)
        if self._group_alive():
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + grace
            while self._group_alive() and time.monotonic() < deadline:
                self.process.poll()
                time.sleep(0.02)
        if self._group_alive():
            raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED")
        try:
            self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED") from None

    def close(self):
        if self.stdout is not None:
            self.stdout.close()


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _SIZE_T = ctypes.c_size_t
    _ULONG_PTR = ctypes.c_size_t

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class _ProcessInfo(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", _SIZE_T), ("MaximumWorkingSetSize", _SIZE_T),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", _ULONG_PTR),
                    ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", _SIZE_T), ("JobMemoryLimit", _SIZE_T),
                    ("PeakProcessMemoryUsed", _SIZE_T), ("PeakJobMemoryUsed", _SIZE_T)]

    class _Accounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in (
            "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime",
            "ThisPeriodTotalKernelTime")] + [(name, wintypes.DWORD) for name in (
                "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses",
                "TotalTerminatedProcesses")]

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    def _signature(name, result, *arguments):
        function = getattr(_kernel, name)
        function.restype, function.argtypes = result, arguments
        return function

    _close_handle = _signature("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _create_job = _signature("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    _set_job = _signature("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE,
                         ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _assign_job = _signature("AssignProcessToJobObject", wintypes.BOOL,
                            wintypes.HANDLE, wintypes.HANDLE)
    _query_job = _signature("QueryInformationJobObject", wintypes.BOOL, wintypes.HANDLE,
                           ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    _terminate_job = _signature("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _terminate_process = _signature("TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _wait = _signature("WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    _exit_code = _signature("GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE,
                           ctypes.POINTER(wintypes.DWORD))
    _open_process = _signature("OpenProcess", wintypes.HANDLE, wintypes.DWORD,
                              wintypes.BOOL, wintypes.DWORD)
    _is_in_job = _signature("IsProcessInJob", wintypes.BOOL, wintypes.HANDLE,
                           wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    _resume = _signature("ResumeThread", wintypes.DWORD, wintypes.HANDLE)
    _create_pipe = _signature("CreatePipe", wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE),
                             ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD)
    _set_handle = _signature("SetHandleInformation", wintypes.BOOL, wintypes.HANDLE,
                            wintypes.DWORD, wintypes.DWORD)
    _create_file = _signature("CreateFileW", wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
                             wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                             wintypes.DWORD, wintypes.HANDLE)
    _init_attributes = _signature("InitializeProcThreadAttributeList", wintypes.BOOL,
                                 ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(_SIZE_T))
    _update_attributes = _signature("UpdateProcThreadAttribute", wintypes.BOOL, ctypes.c_void_p,
                                   wintypes.DWORD, _ULONG_PTR, ctypes.c_void_p, _SIZE_T,
                                   ctypes.c_void_p, ctypes.c_void_p)
    _delete_attributes = _signature("DeleteProcThreadAttributeList", None, ctypes.c_void_p)
    _create_process = _signature(
        "CreateProcessW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(_ProcessInfo),
    )

    def _checked(success, code="EXPERIMENT_PROCESS_CONTAINMENT_FAILED"):
        if not success:
            raise ContainedProcessError(code)

    class _WindowsProcess:
        def __init__(self, command, cwd, env):
            self.job = self.handle = self.thread = None
            self.stdout = None
            self.pid = 0
            read_handle = wintypes.HANDLE()
            write_handle = wintypes.HANDLE()
            null_handle = None
            attributes = None
            attributes_initialized = False
            assigned = False
            info = _ProcessInfo()
            try:
                self.job = _create_job(None, None)  # Unnamed, non-inheritable.
                _checked(self.job)
                limits = _ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE only.
                _checked(_set_job(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
                security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
                _checked(_create_pipe(ctypes.byref(read_handle), ctypes.byref(write_handle),
                                      ctypes.byref(security), 0))
                _checked(_set_handle(read_handle, 1, 0))  # Read side never inherited.
                null_handle = _create_file("NUL", 0x80000000, 3, ctypes.byref(security), 3, 0x80, None)
                _checked(null_handle not in (None, ctypes.c_void_p(-1).value))
                size = _SIZE_T()
                _init_attributes(None, 1, 0, ctypes.byref(size))
                _checked(size.value > 0)
                attributes = ctypes.create_string_buffer(size.value)
                _checked(_init_attributes(attributes, 1, 0, ctypes.byref(size)))
                attributes_initialized = True
                handles = (wintypes.HANDLE * 2)(write_handle.value, null_handle)
                _checked(_update_attributes(attributes, 0, 0x20002, handles,
                                            ctypes.sizeof(handles), None, None))
                startup = _StartupInfoEx()
                startup.StartupInfo.cb = ctypes.sizeof(startup)
                startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
                startup.StartupInfo.hStdInput = null_handle
                startup.StartupInfo.hStdOutput = write_handle.value
                startup.StartupInfo.hStdError = write_handle.value
                startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
                command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command)))
                environment = None
                if env is not None:
                    if any(not isinstance(key, str) or not isinstance(value, str)
                           or not key or "=" in key or "\0" in key or "\0" in value
                           for key, value in env.items()):
                        raise ContainedProcessError("EXPERIMENT_PROCESS_START_FAILED")
                    environment = ctypes.create_unicode_buffer(
                        "\0".join(f"{key}={value}" for key, value in
                                   sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0"
                    )
                flags = 0x4 | 0x400 | 0x80000 | 0x08000000  # SUSPENDED/UNICODE/EX/NOWINDOW
                # Explicit application name avoids ambiguous executable parsing.
                _checked(_create_process(command[0], command_line, None, None, True, flags,
                                         environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)),
                         "EXPERIMENT_PROCESS_START_FAILED")
                self.handle, self.thread, self.pid = info.hProcess, info.hThread, info.dwProcessId
                _checked(_assign_job(self.job, self.handle))
                assigned = True
                fd = msvcrt.open_osfhandle(read_handle.value, os.O_RDONLY | os.O_BINARY)
                read_handle.value = None  # Ownership transferred to the file descriptor.
                self.stdout = os.fdopen(fd, "rb", buffering=0)
            except BaseException:
                # Preserve cleanup even if Ctrl+C arrives between CreateProcessW
                # returning and Python publishing its handles on this object.
                if not self.handle and info.hProcess:
                    self.handle, self.thread, self.pid = info.hProcess, info.hThread, info.dwProcessId
                if self.handle:
                    if assigned:
                        _terminate_job(self.job, 1)
                    else:
                        # Still suspended; no child can have been started.
                        _terminate_process(self.handle, 1)
                    _wait(self.handle, 5000)
                self.close()
                raise
            finally:
                if attributes_initialized:
                    _delete_attributes(attributes)
                for handle in (read_handle.value, write_handle.value, null_handle):
                    if handle and handle != ctypes.c_void_p(-1).value:
                        _close_handle(handle)

        def start(self):
            # Must be the documented initial primary thread's count of exactly 1.
            _checked(_resume(self.thread) == 1)
            _close_handle(self.thread)
            self.thread = None

        def poll(self):
            status = _wait(self.handle, 0)
            if status == 258:  # WAIT_TIMEOUT
                return None
            _checked(status == 0)
            result = wintypes.DWORD()
            _checked(_exit_code(self.handle, ctypes.byref(result)))
            return result.value

        def active_processes(self):
            accounting = _Accounting()
            _checked(_query_job(self.job, 1, ctypes.byref(accounting),
                                ctypes.sizeof(accounting), None))
            return accounting.ActiveProcesses

        def member_handles(self):
            """Pin only this job's current identities, never a broad PID search."""
            capacity = 64
            for _ in range(7):
                class ProcessIds(ctypes.Structure):
                    _fields_ = [("assigned", wintypes.DWORD), ("returned", wintypes.DWORD),
                                ("ids", _ULONG_PTR * capacity)]

                process_ids = ProcessIds()
                okay = _query_job(self.job, 3, ctypes.byref(process_ids),
                                 ctypes.sizeof(process_ids), None)
                if not okay and ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                    raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED")
                if process_ids.returned == process_ids.assigned and okay:
                    break
                capacity = max(capacity * 2, process_ids.assigned)
                if capacity > 4096:
                    raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED")
            else:
                raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED")
            handles = []
            try:
                for pid in process_ids.ids[:process_ids.returned]:
                    # SYNCHRONIZE + PROCESS_QUERY_LIMITED_INFORMATION. A
                    # retained handle, not a recycled PID, is waited below.
                    handle = _open_process(0x101000, False, pid)
                    if not handle:
                        if ctypes.get_last_error() == 87:  # Process already gone.
                            continue
                        raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED")
                    belongs = wintypes.BOOL()
                    if not _is_in_job(handle, self.job, ctypes.byref(belongs)):
                        _close_handle(handle)
                        raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED")
                    if not belongs.value:  # PID was reused between query/open.
                        _close_handle(handle)
                        continue
                    handles.append(handle)
                return handles
            except BaseException:
                for handle in handles:
                    _close_handle(handle)
                raise

        def stop(self, grace):
            members = []
            try:
                try:
                    # At least one existing member means a new association
                    # would exceed 1 and must fail. Do this only during stop,
                    # so the identity snapshot cannot race new executing
                    # descendants; this is not a running-process/thread limit.
                    shutdown_limits = _ExtendedLimits()
                    shutdown_limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x8
                    shutdown_limits.BasicLimitInformation.ActiveProcessLimit = 1
                    _checked(_set_job(self.job, 9, ctypes.byref(shutdown_limits),
                                      ctypes.sizeof(shutdown_limits)),
                             "EXPERIMENT_PROCESS_TERMINATION_FAILED")
                    members = self.member_handles()
                finally:
                    # Even failed identity capture must still kill the private
                    # job. Closing it is a second KILL_ON_JOB_CLOSE fail-safe.
                    _checked(_terminate_job(self.job, 1),
                             "EXPERIMENT_PROCESS_TERMINATION_FAILED")
                deadline = time.monotonic() + grace
                while self.active_processes() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if self.active_processes():
                    raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED")
                # TerminateJobObject is asynchronous. ActiveProcesses alone can
                # reach zero just before a descendant's handle is signaled.
                for handle in [self.handle, *members]:
                    remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                    if _wait(handle, remaining_ms) != 0:
                        raise ContainedProcessError("EXPERIMENT_PROCESS_TERMINATION_FAILED")
            finally:
                for handle in members:
                    _close_handle(handle)

        def close(self):
            # Closing the last non-inherited job handle kills descendants even if
            # Python exits abnormally without reaching explicit stop().
            for name in ("job", "thread", "handle"):
                handle = getattr(self, name, None)
                if handle:
                    _close_handle(handle)
                    setattr(self, name, None)
            if self.stdout is not None:
                self.stdout.close()
