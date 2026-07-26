#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, replace
import math
import os
import signal
import subprocess
import sys
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


TEST_ROOT = REPO_ROOT / "tests"
DEFAULT_TEST_JOBS = 4
DEFAULT_TEST_TIMEOUT_SECONDS = 300.0
PROCESS_POLL_SECONDS = 0.1
DEFAULT_TEST_OUTPUT_MAX_BYTES = 1024 * 1024
OUTPUT_DRAIN_CHUNK_BYTES = 64 * 1024
OUTPUT_DRAIN_FINISH_TIMEOUT_SECONDS = 5.0
OUTPUT_TRUNCATION_MARKER = b"\n... [test output truncated] ...\n"


class RunnerConfigurationError(ValueError):
    """Raised when a test-runner environment setting is invalid."""


class RunnerStopping(RuntimeError):
    """Raised when a worker tries to start after interruption cleanup begins."""


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


class ProcessRegistry:
    """Synchronize subprocess starts with interruption cleanup."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._stopping = False
        self._stop_requested = threading.Event()
        self._cleanup_uncertainties: list[str] = []

    def spawn(self, module: str) -> subprocess.Popen[str]:
        if os.name != "posix":
            raise RunnerConfigurationError(
                "the test runner requires POSIX process-group semantics"
            )
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        with self._lock:
            if self._stopping:
                raise RunnerStopping(
                    f"refusing to start {module}: test runner is stopping"
                )
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
            )
            self._processes[process.pid] = process
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

    def stop_all(self) -> None:
        """Request worker-owned cleanup without racing worker process reaps."""

        with self._lock:
            self._stopping = True
            self._stop_requested.set()


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
    output_parts: list[str] = []
    try:
        stop_result = _stop_process_group(process)
    except Exception as error:
        diagnostic = (
            f"{reason}; cleanup raised "
            f"{type(error).__name__}: {error}"
        )
        registry.record_cleanup_uncertainty(module, diagnostic)
        output_parts.append(f"TEST RUNNER CLEANUP: {diagnostic}")
    else:
        cleanup_confirmed = stop_result.cleanup_confirmed
        output_parts.extend(
            part.rstrip("\n")
            for part in (stop_result.stdout, stop_result.stderr)
            if part
        )
        if stop_result.diagnostic:
            output_parts.append(
                f"TEST RUNNER CLEANUP: {stop_result.diagnostic}"
            )
        if not cleanup_confirmed:
            diagnostic = stop_result.diagnostic or (
                f"{reason}; process-group cleanup could not be confirmed"
            )
            registry.record_cleanup_uncertainty(module, diagnostic)

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
    except BaseException:
        _stop_module_process(
            module,
            process,
            registry,
            timed_out=False,
            reason="output capture setup failure",
        )
        raise

    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if registry.stop_requested():
                return _stop_module_process(
                    module,
                    process,
                    registry,
                    timed_out=False,
                    reason="test-runner interruption",
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
    )


def run_modules(
    modules: list[str],
    *,
    jobs: int,
    timeout_seconds: float,
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
    try:
        for module in ordered_modules:
            future = executor.submit(
                run_module,
                module,
                timeout_seconds,
                registry,
            )
            futures[future] = module
        for future in as_completed(futures):
            module = futures[future]
            try:
                result = future.result()
                if result.passed and result.output:
                    result = replace(result, output="")
                results[module] = result
            except Exception:
                results[module] = _worker_failure(module)
    except BaseException:
        registry.stop_all()
        for future in futures:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    return [results[module] for module in ordered_modules]


def emit_results(
    results: list[ModuleResult],
    *,
    stream: TextIO,
    timeout_seconds: float,
) -> int:
    """Print stable concise status and complete output for failures only."""

    ordered_results = sorted(results, key=lambda result: result.module)
    for result in ordered_results:
        if result.passed:
            print(f"PASS {result.module}", file=stream)
            continue

        if result.timed_out:
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
        f"per-module timeout {timeout_seconds:g}s",
        flush=True,
    )
    registry = ProcessRegistry()
    try:
        with termination_interrupt():
            results = run_modules(
                modules,
                jobs=jobs,
                timeout_seconds=timeout_seconds,
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
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
