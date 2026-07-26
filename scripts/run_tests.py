#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    wait,
)
from contextlib import contextmanager
from dataclasses import dataclass, replace
import math
import os
import select
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import BinaryIO, Iterator, TextIO

from runtime_guard import require_supported_runtime

require_supported_runtime()

from libskillpack import (
    REPO_ROOT,
    _ProcessLeaderState,
    _process_leader_state,
    _stop_process_group,
)
from process_guard.authorization import allow_detached_process


TEST_ROOT = REPO_ROOT / "tests"
PROCESS_GUARD_ROOT = REPO_ROOT / "scripts" / "process_guard"
DEFAULT_TEST_JOBS = 4
DEFAULT_TEST_TIMEOUT_SECONDS = 300.0
DEFAULT_TEST_GLOBAL_TIMEOUT_SECONDS = 360.0
PROCESS_POLL_SECONDS = 0.1
PROCESS_GUARD_READY_TIMEOUT_SECONDS = 3.0
DESCENDANT_CLEANUP_TIMEOUT_SECONDS = 3.0
DESCENDANT_TRACKER_MAX_BYTES = 1024 * 1024
PROCESS_GUARD_ERROR_MAX_BYTES = 64 * 1024
DEFAULT_TEST_OUTPUT_MAX_BYTES = 1024 * 1024
OUTPUT_DRAIN_CHUNK_BYTES = 64 * 1024
OUTPUT_DRAIN_FINISH_TIMEOUT_SECONDS = 5.0
OUTPUT_TRUNCATION_MARKER = b"\n... [test output truncated] ...\n"
GLOBAL_TIMEOUT_REASON = "global test-runner deadline"
TRACKER_FDS_ENV = "STATA_CODEX_TEST_TRACKER_FDS"
STOP_FDS_ENV = "STATA_CODEX_TEST_STOP_FDS"
GUARD_ERROR_PATH_ENV = "STATA_CODEX_TEST_GUARD_ERROR_PATH"


class RunnerConfigurationError(ValueError):
    """Raised when a test-runner environment setting is invalid."""


class RunnerStopping(RuntimeError):
    """Raised when a worker tries to start after interruption cleanup begins."""


class DescendantTracker:
    """Use an inherited pipe as a lease for all module descendants."""

    def __init__(self, descriptor: int) -> None:
        os.set_blocking(descriptor, False)
        self._descriptor = descriptor
        self._lock = threading.Lock()
        self._ready_pids: set[int] = set()
        self._errors: list[str] = []
        self._partial = bytearray()
        self._total_bytes = 0
        self._eof = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._drain,
            name="test-descendant-tracker",
            daemon=True,
        )
        self._thread.start()

    def _record_line(self, line: bytes) -> None:
        fields = line.split()
        if (
            len(fields) == 2
            and fields[0] == b"READY"
            and fields[1].isdigit()
        ):
            pid = int(fields[1])
            if pid > 0:
                with self._lock:
                    self._ready_pids.add(pid)
                return
        with self._lock:
            self._errors.append("malformed process-guard record")

    def _append(self, chunk: bytes) -> None:
        self._total_bytes += len(chunk)
        if self._total_bytes > DESCENDANT_TRACKER_MAX_BYTES:
            with self._lock:
                self._errors.append(
                    "process-guard records exceeded the bounded limit"
                )
            self._stop.set()
            return
        self._partial.extend(chunk)
        while b"\n" in self._partial:
            line, _, remainder = self._partial.partition(b"\n")
            self._partial = bytearray(remainder)
            self._record_line(line)

    def _drain(self) -> None:
        try:
            while not self._stop.is_set():
                readable, _, _ = select.select(
                    [self._descriptor],
                    [],
                    [],
                    PROCESS_POLL_SECONDS,
                )
                if not readable:
                    continue
                chunk = os.read(self._descriptor, 4096)
                if not chunk:
                    self._eof.set()
                    break
                self._append(chunk)
        except BaseException as error:
            with self._lock:
                self._errors.append(
                    "process-guard pipe failed: "
                    f"{type(error).__name__}: {error}"
                )
        finally:
            try:
                os.close(self._descriptor)
            except OSError:
                pass

    def wait_ready(self, pid: int) -> bool:
        deadline = (
            time.monotonic() + PROCESS_GUARD_READY_TIMEOUT_SECONDS
        )
        while time.monotonic() < deadline:
            with self._lock:
                if pid in self._ready_pids:
                    return True
                if self._errors:
                    return False
            if self._eof.wait(PROCESS_POLL_SECONDS):
                break
        with self._lock:
            return pid in self._ready_pids

    def confirm_closed(self, leader_pid: int) -> tuple[bool, str | None]:
        deadline = (
            time.monotonic() + DESCENDANT_CLEANUP_TIMEOUT_SECONDS
        )
        remaining = max(0.0, deadline - time.monotonic())
        reached_eof = self._eof.wait(remaining)
        self.close()
        with self._lock:
            ready = leader_pid in self._ready_pids
            errors = tuple(self._errors)
        diagnostics: list[str] = []
        if not ready:
            diagnostics.append(
                "module process guard did not complete its handshake"
            )
        if not reached_eof:
            diagnostics.append(
                "an escaped or untracked descendant retained the "
                "process lease"
            )
        if self._partial:
            diagnostics.append("process-guard record ended mid-line")
        diagnostics.extend(errors)
        if self._thread.is_alive():
            diagnostics.append("process-guard reader did not stop")
        if diagnostics:
            return False, "; ".join(dict.fromkeys(diagnostics))
        return True, None

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=PROCESS_POLL_SECONDS * 2)


class BoundedOutputCapture:
    """Continuously drain one pipe while retaining a bounded head and tail."""

    def __init__(
        self,
        stream: BinaryIO,
        *,
        max_bytes: int = DEFAULT_TEST_OUTPUT_MAX_BYTES,
    ) -> None:
        if max_bytes < len(OUTPUT_TRUNCATION_MARKER):
            raise ValueError(
                "max_bytes must fit the output truncation marker"
            )
        self._stream = stream
        self._max_bytes = max_bytes
        self._head_limit = max_bytes // 2
        self._tail_limit = max_bytes - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self._total_bytes = 0
        self._lock = threading.Lock()
        self._reader_error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._drain,
            name="test-output-drain",
            daemon=True,
        )

    @classmethod
    def attach(
        cls,
        process: subprocess.Popen[str],
        *,
        max_bytes: int = DEFAULT_TEST_OUTPUT_MAX_BYTES,
    ) -> BoundedOutputCapture:
        """Duplicate the read descriptor so Popen never reaps while draining."""

        if process.stdout is None:
            raise RuntimeError("test process has no stdout pipe")
        duplicate = os.dup(process.stdout.fileno())
        try:
            stream = os.fdopen(duplicate, "rb", buffering=0)
        except BaseException:
            os.close(duplicate)
            raise
        process.stdout.close()
        process.stdout = None
        capture = cls(stream, max_bytes=max_bytes)
        try:
            capture._thread.start()
        except BaseException:
            stream.close()
            raise
        return capture

    def _append(self, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            self._total_bytes += len(data)
            head_room = self._head_limit - len(self._head)
            if head_room > 0:
                head_chunk = data[:head_room]
                self._head.extend(head_chunk)
                data = data[len(head_chunk) :]
            if data:
                self._tail.extend(data)
                overflow = len(self._tail) - self._tail_limit
                if overflow > 0:
                    del self._tail[:overflow]

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(OUTPUT_DRAIN_CHUNK_BYTES)
                if not chunk:
                    break
                self._append(chunk)
        except BaseException as error:
            self._reader_error = error
        finally:
            try:
                self._stream.close()
            except BaseException as error:
                if self._reader_error is None:
                    self._reader_error = error

    def finish(self) -> None:
        """Require the reader to reach EOF after process-group cleanup."""

        self._thread.join(timeout=OUTPUT_DRAIN_FINISH_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass
            raise RuntimeError(
                "test output drain did not reach EOF after process cleanup"
            )
        if self._reader_error is not None:
            raise RuntimeError(
                "test output drain failed: "
                f"{type(self._reader_error).__name__}: "
                f"{self._reader_error}"
            ) from self._reader_error

    def append_text(self, text: str) -> None:
        self._append(text.encode("utf-8", errors="replace"))

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return len(self._head) + len(self._tail)

    def text(self) -> str:
        with self._lock:
            total_bytes = self._total_bytes
            head = bytes(self._head)
            tail = bytes(self._tail)
        if total_bytes <= self._max_bytes:
            rendered = head + tail
        else:
            payload_budget = self._max_bytes - len(
                OUTPUT_TRUNCATION_MARKER
            )
            head_budget = payload_budget // 2
            tail_budget = payload_budget - head_budget
            rendered = (
                head[:head_budget]
                + OUTPUT_TRUNCATION_MARKER
                + (tail[-tail_budget:] if tail_budget else b"")
            )
        return rendered.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class ModuleResult:
    module: str
    returncode: int
    output: str
    timed_out: bool = False
    cleanup_confirmed: bool = True
    global_timed_out: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and self.cleanup_confirmed
        )


def test_command(module: str) -> list[str]:
    """Return the isolated unittest command for one module."""

    module_path = f"{module.replace('.', '/')}.py"
    return [sys.executable, "-m", "unittest", "-v", module_path]


def discover_test_modules() -> list[str]:
    """Discover repository test modules in stable lexical order."""

    return [
        f"tests.{path.stem}"
        for path in sorted(TEST_ROOT.glob("test_*.py"))
        if path.is_file()
    ]


def positive_integer_setting(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RunnerConfigurationError(
            f"{name} must be a positive integer, got {raw_value!r}"
        ) from error
    if value < 1:
        raise RunnerConfigurationError(
            f"{name} must be a positive integer, got {raw_value!r}"
        )
    return value


def positive_timeout_setting(name: str, default: float) -> float:
    raw_value = os.environ.get(name, f"{default:g}").strip()
    try:
        value = float(raw_value)
    except ValueError as error:
        raise RunnerConfigurationError(
            f"{name} must be a finite positive number, got {raw_value!r}"
        ) from error
    if not math.isfinite(value) or value <= 0:
        raise RunnerConfigurationError(
            f"{name} must be a finite positive number, got {raw_value!r}"
        )
    return value


def _descriptor_chain(environment: dict[str, str], name: str) -> list[int]:
    raw_value = environment.get(name, "")
    if not raw_value:
        return []
    try:
        descriptors = [int(value) for value in raw_value.split(",")]
    except ValueError as error:
        raise RunnerConfigurationError(
            f"{name} contains a non-integer descriptor"
        ) from error
    if any(descriptor < 0 for descriptor in descriptors):
        raise RunnerConfigurationError(
            f"{name} contains an invalid descriptor"
        )
    return descriptors


class ProcessRegistry:
    """Synchronize subprocess starts with interruption cleanup."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._stopping = False
        self._stop_requested = threading.Event()
        self._stop_reason: str | None = None
        self._cleanup_uncertainties: list[str] = []

    def spawn(self, module: str) -> subprocess.Popen[str]:
        if os.name != "posix":
            raise RunnerConfigurationError(
                "the test runner requires POSIX process-group semantics"
            )
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        existing_pythonpath = environment.get("PYTHONPATH")
        python_paths = [
            str(PROCESS_GUARD_ROOT),
            str(REPO_ROOT / "scripts"),
        ]
        if existing_pythonpath:
            python_paths.append(existing_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        tracker_descriptors = _descriptor_chain(
            environment,
            TRACKER_FDS_ENV,
        )
        stop_descriptors = _descriptor_chain(
            environment,
            STOP_FDS_ENV,
        )

        tracker_read: int | None = None
        tracker_write: int | None = None
        stop_read: int | None = None
        stop_write: int | None = None
        guard_error_fd: int | None = None
        guard_error_path: str | None = None
        tracker: DescendantTracker | None = None
        process: subprocess.Popen[str] | None = None
        transferred = False
        setup_cleanup_errors: list[str] = []

        def close_descriptor(
            descriptor: int | None,
            label: str,
        ) -> None:
            if descriptor is None:
                return
            try:
                os.close(descriptor)
            except OSError as error:
                setup_cleanup_errors.append(
                    f"could not close {label}: "
                    f"{type(error).__name__}: {error}"
                )

        try:
            tracker_read, tracker_write = os.pipe()
            stop_read, stop_write = os.pipe()
            guard_error_fd, guard_error_path = tempfile.mkstemp(
                prefix="stata-codex-test-guard-",
                suffix=".errors",
            )
            tracker = DescendantTracker(tracker_read)
            tracker_read = None
            tracker_descriptors.append(tracker_write)
            stop_descriptors.append(stop_read)
            environment[TRACKER_FDS_ENV] = ",".join(
                str(descriptor) for descriptor in tracker_descriptors
            )
            environment[STOP_FDS_ENV] = ",".join(
                str(descriptor) for descriptor in stop_descriptors
            )
            environment[GUARD_ERROR_PATH_ENV] = guard_error_path

            with self._lock:
                if self._stopping:
                    raise RunnerStopping(
                        f"refusing to start {module}: "
                        "test runner is stopping"
                    )
                with allow_detached_process():
                    process = subprocess.Popen(
                        test_command(module),
                        cwd=REPO_ROOT,
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        start_new_session=True,
                        pass_fds=tuple(
                            sorted(
                                set(
                                    tracker_descriptors
                                    + stop_descriptors
                                )
                            )
                        ),
                    )
                setattr(process, "_test_guard_required", True)
                setattr(process, "_test_descendant_tracker", tracker)
                setattr(process, "_test_stop_write_fd", stop_write)
                setattr(process, "_test_guard_error_fd", guard_error_fd)
                setattr(process, "_test_guard_error_path", guard_error_path)
                self._processes[process.pid] = process
                transferred = True
        except BaseException:
            if process is not None:
                close_descriptor(
                    tracker_write,
                    "local tracker descriptor",
                )
                tracker_write = None
                close_descriptor(stop_read, "local stop descriptor")
                stop_read = None
                try:
                    _stop_module_process(
                        module,
                        process,
                        self,
                        timed_out=False,
                        reason="module spawn ownership-transfer failure",
                    )
                except BaseException as cleanup_error:
                    self.record_cleanup_uncertainty(
                        module,
                        "module spawn ownership-transfer cleanup raised "
                        f"{type(cleanup_error).__name__}: {cleanup_error}",
                    )
                stop_write = getattr(
                    process,
                    "_test_stop_write_fd",
                    None,
                )
                guard_error_fd = getattr(
                    process,
                    "_test_guard_error_fd",
                    None,
                )
                guard_error_path = getattr(
                    process,
                    "_test_guard_error_path",
                    None,
                )
            raise
        finally:
            close_descriptor(tracker_write, "local tracker descriptor")
            close_descriptor(stop_read, "local stop descriptor")
            if not transferred:
                close_descriptor(stop_write, "descendant stop descriptor")
                close_descriptor(
                    guard_error_fd,
                    "process-guard error descriptor",
                )
                if guard_error_path is not None:
                    try:
                        os.unlink(guard_error_path)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        setup_cleanup_errors.append(
                            "could not remove process-guard error file: "
                            f"{type(error).__name__}: {error}"
                        )
                if tracker is not None:
                    tracker.close()
                else:
                    close_descriptor(
                        tracker_read,
                        "tracker read descriptor",
                    )
            if setup_cleanup_errors:
                self.record_cleanup_uncertainty(
                    module,
                    "; ".join(setup_cleanup_errors),
                )
        return process

    def discard(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    def finish_normally(self, process: subprocess.Popen[str]) -> bool:
        """Linearize normal completion before or after a stop request."""

        with self._lock:
            if self._stopping:
                return False
            return True

    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def stop_reason(self) -> str | None:
        with self._lock:
            return self._stop_reason

    def record_cleanup_uncertainty(
        self,
        module: str,
        diagnostic: str,
    ) -> None:
        with self._lock:
            self._cleanup_uncertainties.append(
                f"{module}: {diagnostic}"
            )

    def cleanup_uncertainties(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._cleanup_uncertainties))

    def stop_all(
        self,
        *,
        reason: str = "test-runner interruption",
    ) -> None:
        """Request worker-owned cleanup without racing worker process reaps."""

        with self._lock:
            self._stopping = True
            if self._stop_reason is None:
                self._stop_reason = reason
            self._stop_requested.set()


def _request_descendant_stop(
    process: subprocess.Popen[str],
) -> tuple[bool, str | None]:
    if not getattr(process, "_test_guard_required", False):
        return True, None
    descriptor = getattr(process, "_test_stop_write_fd", None)
    if descriptor is None:
        return True, None
    setattr(process, "_test_stop_write_fd", None)
    try:
        os.close(descriptor)
    except OSError as error:
        return (
            False,
            "could not close the descendant stop channel: "
            f"{type(error).__name__}: {error}",
        )
    return True, None


def _consume_guard_errors(
    process: subprocess.Popen[str],
) -> tuple[bool, str | None]:
    if not getattr(process, "_test_guard_required", False):
        return True, None
    descriptor = getattr(process, "_test_guard_error_fd", None)
    path = getattr(process, "_test_guard_error_path", None)
    setattr(process, "_test_guard_error_fd", None)
    setattr(process, "_test_guard_error_path", None)
    if not isinstance(descriptor, int) or not isinstance(path, str):
        return False, "module process-guard error channel is unavailable"

    diagnostics: list[str] = []
    payload = bytearray()
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            diagnostics.append(
                "module process-guard error channel changed identity"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        while len(payload) <= PROCESS_GUARD_ERROR_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(
                    4096,
                    PROCESS_GUARD_ERROR_MAX_BYTES + 1 - len(payload),
                ),
            )
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > PROCESS_GUARD_ERROR_MAX_BYTES:
            diagnostics.append(
                "module process-guard errors exceeded the bounded limit"
            )
        if payload:
            diagnostics.append(
                "a descendant could not initialize the process guard"
            )
        try:
            path_metadata = os.lstat(path)
        except FileNotFoundError:
            diagnostics.append(
                "module process-guard error path disappeared"
            )
        else:
            if (
                path_metadata.st_dev,
                path_metadata.st_ino,
            ) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                diagnostics.append(
                    "module process-guard error path changed identity"
                )
            else:
                os.unlink(path)
    except OSError as error:
        diagnostics.append(
            "module process-guard error channel failed: "
            f"{type(error).__name__}: {error}"
        )
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if diagnostics:
        return False, "; ".join(dict.fromkeys(diagnostics))
    return True, None


def _confirm_descendant_cleanup(
    process: subprocess.Popen[str],
) -> tuple[bool, str | None]:
    if not getattr(process, "_test_guard_required", False):
        return True, None
    tracker = getattr(process, "_test_descendant_tracker", None)
    setattr(process, "_test_descendant_tracker", None)
    if not isinstance(tracker, DescendantTracker):
        lease_confirmed = False
        lease_diagnostic = "module descendant tracker is unavailable"
    else:
        lease_confirmed, lease_diagnostic = tracker.confirm_closed(
            process.pid
        )
    guard_confirmed, guard_diagnostic = _consume_guard_errors(process)
    diagnostics = [
        diagnostic
        for diagnostic in (lease_diagnostic, guard_diagnostic)
        if diagnostic
    ]
    return (
        lease_confirmed and guard_confirmed,
        "; ".join(diagnostics) if diagnostics else None,
    )


def _stop_module_process(
    module: str,
    process: subprocess.Popen[str],
    registry: ProcessRegistry,
    *,
    timed_out: bool,
    reason: str,
    capture: BoundedOutputCapture | None = None,
    completed: bool = False,
) -> ModuleResult:
    """Stop one worker-owned group and fail closed on uncertain cleanup."""

    cleanup_confirmed = False
    permission_denied_after_anchored_exit = False
    group_diagnostic = ""
    output_parts: list[str] = []
    try:
        stop_result = _stop_process_group(process)
    except Exception as error:
        group_diagnostic = (
            f"{reason}; cleanup raised "
            f"{type(error).__name__}: {error}"
        )
    else:
        cleanup_confirmed = stop_result.cleanup_confirmed
        permission_denied_after_anchored_exit = (
            stop_result.permission_denied_after_anchored_exit
        )
        group_diagnostic = stop_result.diagnostic
        output_parts.extend(
            part.rstrip("\n")
            for part in (stop_result.stdout, stop_result.stderr)
            if part
        )
    stop_requested, descendant_stop_diagnostic = (
        _request_descendant_stop(process)
    )
    if descendant_stop_diagnostic:
        diagnostic = f"{reason}; {descendant_stop_diagnostic}"
        registry.record_cleanup_uncertainty(module, diagnostic)
        output_parts.append(f"TEST RUNNER CLEANUP: {diagnostic}")
    descendants_confirmed, descendant_diagnostic = (
        _confirm_descendant_cleanup(process)
    )
    guarded_descendants_confirmed = (
        getattr(process, "_test_guard_required", False)
        and stop_requested
        and descendants_confirmed
    )
    if (
        permission_denied_after_anchored_exit
        and guarded_descendants_confirmed
    ):
        cleanup_confirmed = True
        group_diagnostic = ""
    cleanup_confirmed = (
        cleanup_confirmed
        and stop_requested
        and descendants_confirmed
    )
    if not cleanup_confirmed:
        diagnostic = group_diagnostic or (
            f"{reason}; process-group cleanup could not be confirmed"
        )
        registry.record_cleanup_uncertainty(module, diagnostic)
        output_parts.append(f"TEST RUNNER CLEANUP: {diagnostic}")
    if descendant_diagnostic:
        diagnostic = f"{reason}; {descendant_diagnostic}"
        registry.record_cleanup_uncertainty(module, diagnostic)
        output_parts.append(f"TEST RUNNER CLEANUP: {diagnostic}")

    if capture is not None:
        try:
            capture.finish()
        except Exception as error:
            diagnostic = (
                f"{reason}; output cleanup raised "
                f"{type(error).__name__}: {error}"
            )
            registry.record_cleanup_uncertainty(module, diagnostic)
            output_parts.append(f"TEST RUNNER CLEANUP: {diagnostic}")
            cleanup_confirmed = False
        for part in output_parts:
            capture.append_text(part + "\n")
        output = capture.text()
    else:
        output = (
            ("\n".join(output_parts) + "\n")
            if output_parts
            else ""
        )

    if completed and cleanup_confirmed and process.returncode is not None:
        returncode = process.returncode
    else:
        returncode = 124 if timed_out else 1
    return ModuleResult(
        module=module,
        returncode=returncode,
        output=output,
        timed_out=timed_out,
        cleanup_confirmed=cleanup_confirmed,
    )


def run_module(
    module: str,
    timeout_seconds: float,
    registry: ProcessRegistry,
) -> ModuleResult:
    """Run and reap one isolated test module."""

    try:
        process = registry.spawn(module)
    except Exception:
        return ModuleResult(
            module=module,
            returncode=1,
            output=(
                f"Failed to start {module}:\n"
                f"{traceback.format_exc()}"
            ),
        )

    try:
        capture = BoundedOutputCapture.attach(process)
    except Exception:
        exception_output = traceback.format_exc()
        cleanup_result = _stop_module_process(
            module,
            process,
            registry,
            timed_out=False,
            reason="output capture setup failure",
        )
        registry.discard(process)
        return replace(
            cleanup_result,
            returncode=1,
            output=cleanup_result.output + exception_output,
        )
    except BaseException:
        try:
            _stop_module_process(
                module,
                process,
                registry,
                timed_out=False,
                reason="output capture setup failure",
            )
        finally:
            registry.discard(process)
        raise

    try:
        tracker = getattr(process, "_test_descendant_tracker", None)
        if (
            not isinstance(tracker, DescendantTracker)
            or not tracker.wait_ready(process.pid)
        ):
            return _stop_module_process(
                module,
                process,
                registry,
                timed_out=False,
                reason="module process-guard handshake failure",
                capture=capture,
            )
        deadline = time.monotonic() + timeout_seconds
        while True:
            if registry.stop_requested():
                stop_reason = (
                    registry.stop_reason()
                    or "test-runner interruption"
                )
                return _stop_module_process(
                    module,
                    process,
                    registry,
                    timed_out=stop_reason == GLOBAL_TIMEOUT_REASON,
                    reason=stop_reason,
                    capture=capture,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _stop_module_process(
                    module,
                    process,
                    registry,
                    timed_out=True,
                    reason="per-module timeout",
                    capture=capture,
                )
            leader_state = _process_leader_state(process)
            if leader_state is _ProcessLeaderState.UNANCHORED:
                return _stop_module_process(
                    module,
                    process,
                    registry,
                    timed_out=False,
                    reason="lost process-group leader anchor",
                    capture=capture,
                )
            if leader_state is _ProcessLeaderState.EXITED_ANCHORED:
                if not registry.finish_normally(process):
                    return _stop_module_process(
                        module,
                        process,
                        registry,
                        timed_out=False,
                        reason=(
                            "completion raced with test-runner interruption"
                        ),
                        capture=capture,
                    )
                return _stop_module_process(
                    module,
                    process,
                    registry,
                    timed_out=False,
                    reason="normal module completion",
                    capture=capture,
                    completed=True,
                )
            time.sleep(min(PROCESS_POLL_SECONDS, remaining))
    except Exception:
        exception_output = traceback.format_exc()
        cleanup_result = _stop_module_process(
            module,
            process,
            registry,
            timed_out=False,
            reason="worker exception",
            capture=capture,
        )
        return replace(
            cleanup_result,
            returncode=1,
            output=cleanup_result.output + exception_output,
        )
    except BaseException:
        _stop_module_process(
            module,
            process,
            registry,
            timed_out=False,
            reason="worker exception",
            capture=capture,
        )
        raise
    finally:
        registry.discard(process)


def _worker_failure(module: str) -> ModuleResult:
    return ModuleResult(
        module=module,
        returncode=1,
        output=(
            f"Internal test-runner failure for {module}:\n"
            f"{traceback.format_exc()}"
        ),
        cleanup_confirmed=False,
    )


def _global_timeout_result(
    module: str,
    global_timeout_seconds: float,
    *,
    result: ModuleResult | None = None,
    never_started: bool = False,
) -> ModuleResult:
    if never_started:
        return ModuleResult(
            module=module,
            returncode=124,
            output=(
                "TEST RUNNER GLOBAL TIMEOUT: module did not start before "
                f"the {global_timeout_seconds:g}s global deadline; no "
                "process cleanup was required.\n"
            ),
            timed_out=True,
            cleanup_confirmed=True,
            global_timed_out=True,
        )
    if result is None:
        result = _worker_failure(module)
    marker = (
        "TEST RUNNER GLOBAL TIMEOUT: selected module was unfinished at "
        f"the {global_timeout_seconds:g}s global deadline.\n"
    )
    return replace(
        result,
        returncode=124,
        output=marker + result.output,
        timed_out=True,
        global_timed_out=True,
    )


def _completed_future_result(
    module: str,
    future: Future[ModuleResult],
) -> ModuleResult:
    try:
        result = future.result()
    except CancelledError:
        raise
    except Exception:
        return _worker_failure(module)
    if result.passed and result.output:
        return replace(result, output="")
    return result


def run_modules(
    modules: list[str],
    *,
    jobs: int,
    timeout_seconds: float,
    global_timeout_seconds: float,
    registry: ProcessRegistry,
) -> list[ModuleResult]:
    """Run modules concurrently and return results in lexical order."""

    ordered_modules = sorted(dict.fromkeys(modules))
    if not ordered_modules:
        return []
    worker_count = min(jobs, len(ordered_modules))
    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="test-module",
    )
    futures: dict[Future[ModuleResult], str] = {}
    results: dict[str, ModuleResult] = {}
    global_deadline = time.monotonic() + global_timeout_seconds
    shutdown_completed = False
    try:
        for module in ordered_modules:
            future = executor.submit(
                run_module,
                module,
                timeout_seconds,
                registry,
            )
            futures[future] = module
        pending = set(futures)
        while pending:
            remaining = global_deadline - time.monotonic()
            if remaining <= 0:
                break
            completed, _ = wait(
                pending,
                timeout=remaining,
                return_when=FIRST_COMPLETED,
            )
            if not completed:
                break
            for future in completed:
                module = futures[future]
                results[module] = _completed_future_result(module, future)
            pending.difference_update(completed)

        if pending:
            registry.stop_all(reason=GLOBAL_TIMEOUT_REASON)
            for future in pending:
                module = futures[future]
                if future.cancel():
                    results[module] = _global_timeout_result(
                        module,
                        global_timeout_seconds,
                        never_started=True,
                    )
            executor.shutdown(wait=True, cancel_futures=True)
            shutdown_completed = True
            for future in pending:
                module = futures[future]
                if module in results:
                    continue
                try:
                    result = _completed_future_result(module, future)
                except CancelledError:
                    results[module] = _global_timeout_result(
                        module,
                        global_timeout_seconds,
                        never_started=True,
                    )
                    continue
                results[module] = _global_timeout_result(
                    module,
                    global_timeout_seconds,
                    result=result,
                )
    except BaseException:
        registry.stop_all()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        shutdown_completed = True
        raise
    finally:
        if not shutdown_completed:
            executor.shutdown(wait=True)

    if set(results) != set(ordered_modules):
        missing = sorted(set(ordered_modules) - set(results))
        raise RuntimeError(
            "test runner did not aggregate every selected module: "
            + ", ".join(missing)
        )

    return [results[module] for module in ordered_modules]


def emit_results(
    results: list[ModuleResult],
    *,
    stream: TextIO,
    timeout_seconds: float,
    global_timeout_seconds: float,
) -> int:
    """Print stable concise status and complete output for failures only."""

    ordered_results = sorted(results, key=lambda result: result.module)
    for result in ordered_results:
        if result.passed:
            print(f"PASS {result.module}", file=stream)
            continue

        if result.global_timed_out:
            print(
                f"GLOBAL TIMEOUT {result.module} "
                f"(runner limit {global_timeout_seconds:g}s)",
                file=stream,
            )
        elif result.timed_out:
            print(
                f"TIMEOUT {result.module} "
                f"(limit {timeout_seconds:g}s)",
                file=stream,
            )
        else:
            print(
                f"FAIL {result.module} "
                f"(exit {result.returncode})",
                file=stream,
            )
        print(f"--- output: {result.module} ---", file=stream)
        if result.output:
            stream.write(result.output)
            if not result.output.endswith("\n"):
                stream.write("\n")
        else:
            print("(no output)", file=stream)
        print(f"--- end output: {result.module} ---", file=stream)

    passed = sum(result.passed for result in ordered_results)
    timed_out = sum(result.timed_out for result in ordered_results)
    failed = len(ordered_results) - passed
    print(
        f"SUMMARY {passed} passed, {failed} failed, "
        f"{timed_out} timed out, {len(ordered_results)} modules total",
        file=stream,
    )
    return failed


def _raise_keyboard_interrupt(
    _signal_number: int,
    _frame: object,
) -> None:
    raise KeyboardInterrupt


@contextmanager
def termination_interrupt() -> Iterator[None]:
    """Translate SIGTERM into the same cleanup path as Ctrl-C on POSIX."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def main() -> int:
    if os.name != "posix":
        print(
            "ERROR: test runner requires POSIX process-group semantics",
            file=sys.stderr,
        )
        return 2
    try:
        jobs = positive_integer_setting(
            "TEST_JOBS",
            DEFAULT_TEST_JOBS,
        )
        timeout_seconds = positive_timeout_setting(
            "TEST_TIMEOUT",
            DEFAULT_TEST_TIMEOUT_SECONDS,
        )
        global_timeout_seconds = positive_timeout_setting(
            "TEST_GLOBAL_TIMEOUT",
            DEFAULT_TEST_GLOBAL_TIMEOUT_SECONDS,
        )
    except RunnerConfigurationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    modules = discover_test_modules()
    if not modules:
        print("ERROR: no test modules discovered", file=sys.stderr)
        return 2

    print(
        f"Running {len(modules)} test modules with "
        f"{min(jobs, len(modules))} worker(s); "
        f"per-module timeout {timeout_seconds:g}s; "
        f"global timeout {global_timeout_seconds:g}s",
        flush=True,
    )
    registry = ProcessRegistry()
    try:
        with termination_interrupt():
            results = run_modules(
                modules,
                jobs=jobs,
                timeout_seconds=timeout_seconds,
                global_timeout_seconds=global_timeout_seconds,
                registry=registry,
            )
    except KeyboardInterrupt:
        registry.stop_all()
        cleanup_uncertainties = registry.cleanup_uncertainties()
        for diagnostic in cleanup_uncertainties:
            print(
                f"CLEANUP UNCERTAIN: {diagnostic}",
                file=sys.stderr,
            )
        if cleanup_uncertainties:
            print(
                "INTERRUPTED: test subprocess cleanup could not be "
                "fully confirmed",
                file=sys.stderr,
            )
        else:
            print(
                "INTERRUPTED: test subprocess cleanup completed",
                file=sys.stderr,
            )
        return 130

    failures = emit_results(
        results,
        stream=sys.stdout,
        timeout_seconds=timeout_seconds,
        global_timeout_seconds=global_timeout_seconds,
    )
    cleanup_uncertainties = registry.cleanup_uncertainties()
    for diagnostic in cleanup_uncertainties:
        print(f"CLEANUP UNCERTAIN: {diagnostic}", file=sys.stderr)
    return 1 if failures or cleanup_uncertainties else 0


if __name__ == "__main__":
    raise SystemExit(main())
