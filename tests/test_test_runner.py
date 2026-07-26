from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_tests  # noqa: E402
import libskillpack  # noqa: E402
from process_guard.authorization import allow_detached_process  # noqa: E402


class TestRunnerTests(unittest.TestCase):
    def test_default_test_jobs_is_four(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                4,
                run_tests.positive_integer_setting(
                    "TEST_JOBS",
                    run_tests.DEFAULT_TEST_JOBS,
                ),
            )
            self.assertEqual(
                360,
                run_tests.positive_timeout_setting(
                    "TEST_GLOBAL_TIMEOUT",
                    run_tests.DEFAULT_TEST_GLOBAL_TIMEOUT_SECONDS,
                ),
            )

    def test_ci_budget_leaves_bounded_cleanup_and_build_headroom(
        self,
    ) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        make_match = re.search(
            r"^TEST_GLOBAL_TIMEOUT\s*\?=\s*(\d+)\s*$",
            makefile,
            flags=re.MULTILINE,
        )
        self.assertIsNotNone(make_match)
        assert make_match is not None
        make_timeout_seconds = int(make_match.group(1))
        self.assertEqual(
            run_tests.DEFAULT_TEST_GLOBAL_TIMEOUT_SECONDS,
            make_timeout_seconds,
        )

        workflow = (
            REPO_ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        match = re.search(r"timeout-minutes:\s*(\d+)", workflow)
        self.assertIsNotNone(match)
        assert match is not None
        ci_seconds = int(match.group(1)) * 60
        self.assertGreaterEqual(
            ci_seconds - make_timeout_seconds,
            300,
        )

    def test_success_is_concise_and_returns_zero(self) -> None:
        results = [
            run_tests.ModuleResult("tests.test_zeta", 0, "zeta noise\n"),
            run_tests.ModuleResult("tests.test_alpha", 0, "alpha noise\n"),
        ]
        output = io.StringIO()
        errors = io.StringIO()

        with patch.dict(
            os.environ,
            {
                "TEST_JOBS": "4",
                "TEST_TIMEOUT": "12",
                "TEST_GLOBAL_TIMEOUT": "90",
            },
        ), patch.object(
            run_tests,
            "discover_test_modules",
            return_value=["tests.test_zeta", "tests.test_alpha"],
        ), patch.object(
            run_tests,
            "run_modules",
            return_value=results,
        ) as run_modules, redirect_stdout(output), redirect_stderr(errors):
            exit_code = run_tests.main()

        self.assertEqual(0, exit_code)
        self.assertEqual("", errors.getvalue())
        rendered = output.getvalue()
        self.assertLess(
            rendered.index("PASS tests.test_alpha"),
            rendered.index("PASS tests.test_zeta"),
        )
        self.assertNotIn("alpha noise", rendered)
        self.assertNotIn("zeta noise", rendered)
        self.assertIn(
            "SUMMARY 2 passed, 0 failed, 0 timed out, 2 modules total",
            rendered,
        )
        self.assertEqual(4, run_modules.call_args.kwargs["jobs"])
        self.assertEqual(
            12,
            run_modules.call_args.kwargs["timeout_seconds"],
        )
        self.assertEqual(
            90,
            run_modules.call_args.kwargs["global_timeout_seconds"],
        )
        self.assertIn("global timeout 90s", rendered)

    def test_failures_are_aggregated_with_stable_full_output(self) -> None:
        results = [
            run_tests.ModuleResult(
                "tests.test_zeta",
                7,
                "zeta stdout\nzeta stderr\n",
            ),
            run_tests.ModuleResult(
                "tests.test_alpha",
                0,
                "successful output must stay hidden\n",
            ),
            run_tests.ModuleResult(
                "tests.test_middle",
                -15,
                "middle failure without newline",
            ),
        ]
        output = io.StringIO()

        with patch.dict(
            os.environ,
            {"TEST_JOBS": "3", "TEST_TIMEOUT": "30"},
        ), patch.object(
            run_tests,
            "discover_test_modules",
            return_value=[
                "tests.test_zeta",
                "tests.test_alpha",
                "tests.test_middle",
            ],
        ), patch.object(
            run_tests,
            "run_modules",
            return_value=results,
        ), redirect_stdout(output), redirect_stderr(io.StringIO()):
            exit_code = run_tests.main()

        self.assertEqual(1, exit_code)
        rendered = output.getvalue()
        alpha = rendered.index("PASS tests.test_alpha")
        middle = rendered.index("FAIL tests.test_middle")
        zeta = rendered.index("FAIL tests.test_zeta")
        self.assertLess(alpha, middle)
        self.assertLess(middle, zeta)
        self.assertNotIn("successful output must stay hidden", rendered)
        self.assertIn("middle failure without newline", rendered)
        self.assertIn("zeta stdout\nzeta stderr\n", rendered)
        self.assertIn("SUMMARY 1 passed, 2 failed", rendered)

    def test_invalid_test_jobs_fails_before_discovery(self) -> None:
        for value in ("", "0", "-1", "nope", "1.5"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"TEST_JOBS": value, "TEST_TIMEOUT": "30"},
            ), patch.object(
                run_tests,
                "discover_test_modules",
            ) as discover, redirect_stdout(io.StringIO()), redirect_stderr(
                io.StringIO()
            ):
                exit_code = run_tests.main()

            self.assertEqual(2, exit_code)
            discover.assert_not_called()

    def test_invalid_global_timeout_fails_before_discovery(self) -> None:
        for value in ("", "0", "-1", "nope", "inf"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {
                    "TEST_JOBS": "1",
                    "TEST_TIMEOUT": "30",
                    "TEST_GLOBAL_TIMEOUT": value,
                },
            ), patch.object(
                run_tests,
                "discover_test_modules",
            ) as discover, redirect_stdout(io.StringIO()), redirect_stderr(
                io.StringIO()
            ):
                exit_code = run_tests.main()

            self.assertEqual(2, exit_code)
            discover.assert_not_called()

    def test_non_posix_fails_before_discovery_or_process_start(self) -> None:
        errors = io.StringIO()
        with patch.object(
            run_tests.os,
            "name",
            "nt",
        ), patch.object(
            run_tests,
            "discover_test_modules",
        ) as discover, patch.object(
            run_tests.subprocess,
            "Popen",
        ) as popen, redirect_stdout(io.StringIO()), redirect_stderr(errors):
            exit_code = run_tests.main()

        self.assertEqual(2, exit_code)
        self.assertIn("requires POSIX", errors.getvalue())
        discover.assert_not_called()
        popen.assert_not_called()

    def test_spawn_rejects_bad_inherited_descriptors_before_allocating(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {run_tests.TRACKER_FDS_ENV: "not-an-integer"},
        ), patch.object(
            run_tests.os,
            "pipe",
        ) as pipe, patch.object(
            run_tests.tempfile,
            "mkstemp",
        ) as mkstemp:
            with self.assertRaises(run_tests.RunnerConfigurationError):
                run_tests.ProcessRegistry().spawn("tests.test_invalid_guard")

        pipe.assert_not_called()
        mkstemp.assert_not_called()

    def test_spawn_prefix_failure_releases_descriptors_and_tempfile(
        self,
    ) -> None:
        real_pipe = os.pipe
        real_mkstemp = run_tests.tempfile.mkstemp
        opened_descriptors: list[int] = []

        def tracked_pipe() -> tuple[int, int]:
            descriptors = real_pipe()
            opened_descriptors.extend(descriptors)
            return descriptors

        with TemporaryDirectory(
            prefix="test-runner-prefix-failure-"
        ) as temporary:
            root = Path(temporary)

            def local_mkstemp(*, prefix: str, suffix: str) -> tuple[int, str]:
                descriptor, path = real_mkstemp(
                    prefix=prefix,
                    suffix=suffix,
                    dir=root,
                )
                opened_descriptors.append(descriptor)
                return descriptor, path

            with patch.object(
                run_tests.os,
                "pipe",
                side_effect=tracked_pipe,
            ), patch.object(
                run_tests.tempfile,
                "mkstemp",
                side_effect=local_mkstemp,
            ), patch.object(
                run_tests,
                "DescendantTracker",
                side_effect=RuntimeError("synthetic tracker failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "synthetic tracker failure",
                ):
                    run_tests.ProcessRegistry().spawn(
                        "tests.test_prefix_failure"
                    )

            self.assertEqual([], list(root.iterdir()))
            for descriptor in opened_descriptors:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    @unittest.skipUnless(os.name == "posix", "POSIX descendant containment")
    def test_spawn_transfer_failure_stops_detached_python_descendant(
        self,
    ) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import os
            import subprocess
            import sys
            import time
            from process_guard.authorization import allow_detached_process

            with allow_detached_process():
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            Path(sys.argv[1]).write_text(
                f"{child.pid}\\n",
                encoding="utf-8",
            )
            time.sleep(60)
            """
        )

        with TemporaryDirectory(
            prefix="test-runner-transfer-failure-"
        ) as temporary:
            root = Path(temporary)
            helper = root / "transfer_failure_helper.py"
            pid_path = root / "child.pid"
            helper.write_text(helper_source, encoding="utf-8")

            class RejectingRegistry(dict[int, subprocess.Popen[str]]):
                def __setitem__(
                    self,
                    key: int,
                    value: subprocess.Popen[str],
                ) -> None:
                    deadline = time.monotonic() + 5
                    while (
                        not pid_path.exists()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    raise RuntimeError("synthetic registry failure")

            registry = run_tests.ProcessRegistry()
            registry._processes = RejectingRegistry()
            with patch.object(
                run_tests,
                "test_command",
                return_value=[
                    sys.executable,
                    str(helper),
                    str(pid_path),
                ],
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "synthetic registry failure",
                ):
                    registry.spawn("tests.test_transfer_failure")

            child_pid = int(pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail(f"detached child {child_pid} still exists")
            self.assertEqual((), registry.cleanup_uncertainties())

    @unittest.skipUnless(os.name == "posix", "POSIX descendant containment")
    def test_spawn_transfer_failure_records_uncooperative_descendant(
        self,
    ) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import subprocess
            import sys
            import time
            from process_guard.authorization import allow_detached_process

            with allow_detached_process():
                child = subprocess.Popen(
                    ["/bin/sleep", "60"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            Path(sys.argv[1]).write_text(
                f"{child.pid}\\n",
                encoding="utf-8",
            )
            time.sleep(60)
            """
        )

        with TemporaryDirectory(
            prefix="test-runner-transfer-uncooperative-"
        ) as temporary:
            root = Path(temporary)
            helper = root / "transfer_uncooperative_helper.py"
            pid_path = root / "child.pid"
            helper.write_text(helper_source, encoding="utf-8")

            class RejectingRegistry(dict[int, subprocess.Popen[str]]):
                def __setitem__(
                    self,
                    key: int,
                    value: subprocess.Popen[str],
                ) -> None:
                    deadline = time.monotonic() + 5
                    while (
                        not pid_path.exists()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    raise RuntimeError("synthetic registry failure")

            registry = run_tests.ProcessRegistry()
            registry._processes = RejectingRegistry()
            child_pid: int | None = None
            try:
                with patch.object(
                    run_tests,
                    "test_command",
                    return_value=[
                        sys.executable,
                        str(helper),
                        str(pid_path),
                    ],
                ), patch.object(
                    run_tests,
                    "DESCENDANT_CLEANUP_TIMEOUT_SECONDS",
                    0.2,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "synthetic registry failure",
                    ):
                        registry.spawn(
                            "tests.test_transfer_uncooperative"
                        )

                child_pid = int(pid_path.read_text(encoding="utf-8"))
                self.assertTrue(registry.cleanup_uncertainties())
                self.assertIn(
                    "escaped or untracked descendant",
                    registry.cleanup_uncertainties()[0],
                )
                os.kill(child_pid, 0)
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_interruption_does_not_claim_uncertain_cleanup_completed(
        self,
    ) -> None:
        registry = run_tests.ProcessRegistry()
        registry.record_cleanup_uncertainty(
            "tests.test_uncertain",
            "process group could not be confirmed",
        )
        errors = io.StringIO()
        with patch.dict(
            os.environ,
            {"TEST_JOBS": "1", "TEST_TIMEOUT": "30"},
        ), patch.object(
            run_tests,
            "discover_test_modules",
            return_value=["tests.test_uncertain"],
        ), patch.object(
            run_tests,
            "ProcessRegistry",
            return_value=registry,
        ), patch.object(
            run_tests,
            "run_modules",
            side_effect=KeyboardInterrupt,
        ), redirect_stdout(io.StringIO()), redirect_stderr(errors):
            exit_code = run_tests.main()

        self.assertEqual(130, exit_code)
        rendered = errors.getvalue()
        self.assertIn("CLEANUP UNCERTAIN", rendered)
        self.assertIn("could not be fully confirmed", rendered)
        self.assertNotIn("cleanup completed", rendered)

    @unittest.skipUnless(os.name == "posix", "POSIX pipe draining")
    def test_output_capture_is_bounded_with_stable_head_and_tail(self) -> None:
        captures: list[run_tests.BoundedOutputCapture] = []
        original_attach = run_tests.BoundedOutputCapture.attach

        def attach_with_small_limit(
            process: subprocess.Popen[str],
            *,
            max_bytes: int = run_tests.DEFAULT_TEST_OUTPUT_MAX_BYTES,
        ) -> run_tests.BoundedOutputCapture:
            del max_bytes
            capture = original_attach(process, max_bytes=128)
            captures.append(capture)
            return capture

        source = (
            "import sys; "
            "sys.stdout.write('HEAD-' + ('middle-' * 2048) + '-TAIL')"
        )
        with patch.object(
            run_tests,
            "test_command",
            return_value=[sys.executable, "-c", source],
        ), patch.object(
            run_tests.BoundedOutputCapture,
            "attach",
            side_effect=attach_with_small_limit,
        ):
            result = run_tests.run_module(
                "tests.noisy_helper",
                5,
                run_tests.ProcessRegistry(),
            )

        self.assertTrue(result.passed, result.output)
        encoded = result.output.encode("utf-8")
        self.assertLessEqual(len(encoded), 128)
        self.assertTrue(result.output.startswith("HEAD-"))
        self.assertTrue(result.output.endswith("-TAIL"))
        self.assertIn("test output truncated", result.output)
        self.assertEqual(1, len(captures))
        self.assertLessEqual(captures[0].retained_bytes, 128)

    def test_serial_and_parallel_worker_selection(self) -> None:
        serial_active = 0
        serial_maximum = 0
        serial_starts: list[str] = []
        serial_lock = threading.Lock()

        def serial_worker(
            module: str,
            _timeout: float,
            _registry: run_tests.ProcessRegistry,
        ) -> run_tests.ModuleResult:
            nonlocal serial_active, serial_maximum
            with serial_lock:
                serial_active += 1
                serial_maximum = max(serial_maximum, serial_active)
                serial_starts.append(module)
            time.sleep(0.01)
            with serial_lock:
                serial_active -= 1
            return run_tests.ModuleResult(module, 0, "successful noise")

        with patch.object(run_tests, "run_module", side_effect=serial_worker):
            serial_results = run_tests.run_modules(
                ["tests.test_c", "tests.test_a", "tests.test_b"],
                jobs=1,
                timeout_seconds=5,
                global_timeout_seconds=5,
                registry=run_tests.ProcessRegistry(),
            )

        self.assertEqual(1, serial_maximum)
        self.assertEqual(
            ["tests.test_a", "tests.test_b", "tests.test_c"],
            serial_starts,
        )
        self.assertEqual(
            ["tests.test_a", "tests.test_b", "tests.test_c"],
            [result.module for result in serial_results],
        )
        self.assertTrue(all(not result.output for result in serial_results))

        parallel_active = 0
        parallel_maximum = 0
        parallel_lock = threading.Lock()
        both_started = threading.Event()
        release = threading.Event()

        def parallel_worker(
            module: str,
            _timeout: float,
            _registry: run_tests.ProcessRegistry,
        ) -> run_tests.ModuleResult:
            nonlocal parallel_active, parallel_maximum
            with parallel_lock:
                parallel_active += 1
                parallel_maximum = max(
                    parallel_maximum,
                    parallel_active,
                )
                if parallel_active == 2:
                    both_started.set()
            both_started.wait(timeout=1)
            release.wait(timeout=1)
            with parallel_lock:
                parallel_active -= 1
            return run_tests.ModuleResult(module, 0, "")

        def release_workers() -> None:
            both_started.wait(timeout=1)
            release.set()

        controller = threading.Thread(target=release_workers)
        controller.start()
        with patch.object(
            run_tests,
            "run_module",
            side_effect=parallel_worker,
        ):
            parallel_results = run_tests.run_modules(
                ["tests.test_b", "tests.test_a"],
                jobs=2,
                timeout_seconds=5,
                global_timeout_seconds=5,
                registry=run_tests.ProcessRegistry(),
            )
        controller.join(timeout=1)

        self.assertFalse(controller.is_alive())
        self.assertTrue(both_started.is_set())
        self.assertEqual(2, parallel_maximum)
        self.assertEqual(
            ["tests.test_a", "tests.test_b"],
            [result.module for result in parallel_results],
        )

    def test_worker_exception_fails_cleanup_closed(self) -> None:
        registry = run_tests.ProcessRegistry()

        def broken_worker(
            module: str,
            _timeout: float,
            worker_registry: run_tests.ProcessRegistry,
        ) -> run_tests.ModuleResult:
            worker_registry.record_cleanup_uncertainty(
                module,
                "synthetic cleanup uncertainty",
            )
            raise RuntimeError("synthetic worker failure")

        with patch.object(
            run_tests,
            "run_module",
            side_effect=broken_worker,
        ):
            results = run_tests.run_modules(
                ["tests.test_broken"],
                jobs=1,
                timeout_seconds=5,
                global_timeout_seconds=5,
                registry=registry,
            )

        self.assertEqual(1, len(results))
        self.assertFalse(results[0].passed)
        self.assertFalse(results[0].cleanup_confirmed)
        self.assertIn("synthetic worker failure", results[0].output)
        self.assertTrue(registry.cleanup_uncertainties())

    def test_normal_exit_reports_registry_cleanup_uncertainty(self) -> None:
        registry = run_tests.ProcessRegistry()
        registry.record_cleanup_uncertainty(
            "tests.test_uncertain",
            "synthetic cleanup uncertainty",
        )
        errors = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "TEST_JOBS": "1",
                "TEST_TIMEOUT": "30",
                "TEST_GLOBAL_TIMEOUT": "60",
            },
        ), patch.object(
            run_tests,
            "discover_test_modules",
            return_value=["tests.test_uncertain"],
        ), patch.object(
            run_tests,
            "ProcessRegistry",
            return_value=registry,
        ), patch.object(
            run_tests,
            "run_modules",
            return_value=[
                run_tests.ModuleResult("tests.test_uncertain", 0, "")
            ],
        ), redirect_stdout(io.StringIO()), redirect_stderr(errors):
            exit_code = run_tests.main()

        self.assertEqual(1, exit_code)
        self.assertIn("CLEANUP UNCERTAIN", errors.getvalue())

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    def test_global_deadline_cancels_queued_and_cleans_active_group(
        self,
    ) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import os
            import sys
            import time

            Path(sys.argv[1]).write_text(
                f"{os.getpid()}\\n",
                encoding="utf-8",
            )
            time.sleep(60)
            """
        )

        with TemporaryDirectory(prefix="test-runner-global-timeout-") as temporary:
            root = Path(temporary)
            helper = root / "global_timeout_helper.py"
            helper.write_text(helper_source, encoding="utf-8")
            pid_paths = {
                "tests.test_a_active": root / "active.pid",
                "tests.test_z_queued": root / "queued.pid",
            }

            def command(module: str) -> list[str]:
                return [
                    sys.executable,
                    str(helper),
                    str(pid_paths[module]),
                ]

            registry = run_tests.ProcessRegistry()
            started = time.monotonic()
            with patch.object(run_tests, "test_command", side_effect=command):
                results = run_tests.run_modules(
                    list(reversed(pid_paths)),
                    jobs=1,
                    timeout_seconds=30,
                    global_timeout_seconds=1,
                    registry=registry,
                )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 5)
            self.assertEqual(
                sorted(pid_paths),
                [result.module for result in results],
            )
            self.assertEqual(2, len(results))
            self.assertTrue(all(result.global_timed_out for result in results))
            self.assertTrue(all(result.timed_out for result in results))
            self.assertTrue(
                all(result.cleanup_confirmed for result in results),
                results,
            )
            self.assertIn("unfinished", results[0].output)
            self.assertIn("did not start", results[1].output)
            self.assertTrue(pid_paths["tests.test_a_active"].is_file())
            self.assertFalse(pid_paths["tests.test_z_queued"].exists())
            self.assertEqual((), registry.cleanup_uncertainties())
            output = io.StringIO()
            self.assertEqual(
                2,
                run_tests.emit_results(
                    results,
                    stream=output,
                    timeout_seconds=30,
                    global_timeout_seconds=1,
                ),
            )
            self.assertEqual(
                2,
                output.getvalue().count("GLOBAL TIMEOUT tests."),
            )
            self.assertIn(
                "SUMMARY 0 passed, 2 failed, 2 timed out, "
                "2 modules total",
                output.getvalue(),
            )

            process_id = int(
                pid_paths["tests.test_a_active"].read_text(encoding="utf-8")
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(process_id, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail(
                    f"globally timed-out process {process_id} still exists"
                )

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    def test_timeout_terminates_child_group_and_reaps_processes(self) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import os
            import subprocess
            import sys
            import time

            pid_path = Path(sys.argv[1])
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )

            pid_path.write_text(
                f"{os.getpid()} {child.pid}\\n",
                encoding="utf-8",
            )
            while True:
                time.sleep(1)
            """
        )

        with TemporaryDirectory(prefix="test-runner-timeout-") as temporary:
            root = Path(temporary)
            helper = root / "timeout_helper.py"
            pid_path = root / "pids.txt"
            helper.write_text(helper_source, encoding="utf-8")

            with patch.object(
                run_tests,
                "test_command",
                return_value=[
                    sys.executable,
                    str(helper),
                    str(pid_path),
                ],
            ):
                result = run_tests.run_module(
                    "tests.timeout_helper",
                    1,
                    run_tests.ProcessRegistry(),
                )

            self.assertTrue(result.timed_out)
            self.assertNotEqual(0, result.returncode)
            self.assertTrue(result.cleanup_confirmed, result.output)
            parent_pid, child_pid = (
                int(value)
                for value in pid_path.read_text(
                    encoding="utf-8"
                ).split()
            )
            for process_id in (parent_pid, child_pid):
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(process_id, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                else:
                    self.fail(
                        f"timed-out process {process_id} still exists"
                    )

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    def test_zero_exit_cleans_redirected_same_group_child(self) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import os
            import subprocess
            import sys

            pid_path = Path(sys.argv[1])
            child_source = '''
            import time

            while True:
                time.sleep(1)
            '''
            child = subprocess.Popen(
                [sys.executable, "-c", child_source],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pid_path.write_text(
                f"{os.getpid()} {child.pid}\\n",
                encoding="utf-8",
            )
            os._exit(0)
            """
        )

        with TemporaryDirectory(prefix="test-runner-orphan-") as temporary:
            root = Path(temporary)
            helper = root / "orphan_helper.py"
            pid_path = root / "pids.txt"
            helper.write_text(helper_source, encoding="utf-8")

            with patch.object(
                run_tests,
                "test_command",
                return_value=[
                    sys.executable,
                    str(helper),
                    str(pid_path),
                ],
            ):
                result = run_tests.run_module(
                    "tests.orphan_helper",
                    1,
                    run_tests.ProcessRegistry(),
                )

            self.assertTrue(result.passed, result.output)
            self.assertFalse(result.timed_out)
            self.assertTrue(result.cleanup_confirmed, result.output)
            _parent_pid, child_pid = (
                int(value)
                for value in pid_path.read_text(
                    encoding="utf-8"
                ).split()
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail(
                    f"orphaned timed-out child {child_pid} still exists"
                )

    @unittest.skipUnless(os.name == "posix", "POSIX descendant containment")
    def test_unreviewed_positional_detachment_is_denied(self) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import subprocess
            import sys

            try:
                subprocess.Popen(
                    [sys.executable, "-c", "pass"],
                    -1, None, None,
                    subprocess.DEVNULL, subprocess.DEVNULL,
                    None, True, False, None, None, None, None,
                    0, True, True, (),
                )
            except PermissionError:
                Path(sys.argv[1]).write_text(
                    "denied\\n",
                    encoding="utf-8",
                )
            else:
                raise RuntimeError("unguarded detachment was accepted")
            """
        )

        with TemporaryDirectory(
            prefix="test-runner-denied-detach-"
        ) as temporary:
            root = Path(temporary)
            helper = root / "denied_detach_helper.py"
            result_path = root / "result.txt"
            helper.write_text(helper_source, encoding="utf-8")

            with patch.object(
                run_tests,
                "test_command",
                return_value=[
                    sys.executable,
                    str(helper),
                    str(result_path),
                ],
            ):
                result = run_tests.run_module(
                    "tests.denied_detach_helper",
                    5,
                    run_tests.ProcessRegistry(),
                )

            self.assertTrue(result.passed, result.output)
            self.assertEqual(
                "denied\n",
                result_path.read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(os.name == "posix", "POSIX descendant containment")
    def test_zero_exit_cleans_detached_python_child(self) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import os
            import subprocess
            import sys
            from process_guard.authorization import allow_detached_process

            with allow_detached_process():
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(60)",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            Path(sys.argv[1]).write_text(
                f"{child.pid}\\n",
                encoding="utf-8",
            )
            os._exit(0)
            """
        )

        with TemporaryDirectory(
            prefix="test-runner-detached-"
        ) as temporary:
            root = Path(temporary)
            helper = root / "detached_helper.py"
            pid_path = root / "child.pid"
            helper.write_text(helper_source, encoding="utf-8")

            with patch.object(
                run_tests,
                "test_command",
                return_value=[
                    sys.executable,
                    str(helper),
                    str(pid_path),
                ],
            ):
                result = run_tests.run_module(
                    "tests.detached_helper",
                    5,
                    run_tests.ProcessRegistry(),
                )

            self.assertTrue(result.passed, result.output)
            self.assertTrue(result.cleanup_confirmed, result.output)
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail(f"detached child {child_pid} still exists")

    @unittest.skipUnless(os.name == "posix", "POSIX descendant containment")
    def test_multiprocessing_spawn_cannot_escape_guard(self) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import multiprocessing
            import os
            import sys
            import time

            def target(pid_path):
                os.setsid()
                Path(pid_path).write_text(
                    f"{os.getpid()}\\n",
                    encoding="utf-8",
                )
                time.sleep(60)

            if __name__ == "__main__":
                context = multiprocessing.get_context("spawn")
                child = context.Process(
                    target=target,
                    args=(sys.argv[2],),
                )
                child.start()
                child.join(timeout=5)
                Path(sys.argv[1]).write_text(
                    f"{child.pid} {child.exitcode}\\n",
                    encoding="utf-8",
                )
                os._exit(0)
            """
        )

        with TemporaryDirectory(
            prefix="test-runner-multiprocessing-"
        ) as temporary:
            root = Path(temporary)
            helper = root / "multiprocessing_helper.py"
            result_path = root / "result.txt"
            escaped_path = root / "escaped.pid"
            helper.write_text(helper_source, encoding="utf-8")

            with patch.object(
                run_tests,
                "test_command",
                return_value=[
                    sys.executable,
                    str(helper),
                    str(result_path),
                    str(escaped_path),
                ],
            ):
                result = run_tests.run_module(
                    "tests.multiprocessing_helper",
                    10,
                    run_tests.ProcessRegistry(),
                )

            self.assertTrue(result.passed, result.output)
            child_pid, exit_code = (
                int(value)
                for value in result_path.read_text(
                    encoding="utf-8"
                ).split()
            )
            self.assertNotEqual(0, exit_code)
            self.assertFalse(escaped_path.exists())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail(
                    f"multiprocessing child {child_pid} still exists"
                )

    @unittest.skipUnless(os.name == "posix", "POSIX descendant containment")
    def test_descendant_guard_initialization_failure_fails_gate(self) -> None:
        helper_source = textwrap.dedent(
            """
            import os
            import subprocess
            import sys

            environment = os.environ.copy()
            environment["STATA_CODEX_TEST_TRACKER_FDS"] = "999999"
            subprocess.run(
                [sys.executable, "-c", "pass"],
                check=False,
                env=environment,
            )
            os._exit(0)
            """
        )

        with TemporaryDirectory(
            prefix="test-runner-guard-init-"
        ) as temporary:
            helper = Path(temporary) / "guard_init_helper.py"
            helper.write_text(helper_source, encoding="utf-8")

            with patch.object(
                run_tests,
                "test_command",
                return_value=[sys.executable, str(helper)],
            ):
                result = run_tests.run_module(
                    "tests.guard_init_helper",
                    5,
                    run_tests.ProcessRegistry(),
                )

            self.assertFalse(result.passed)
            self.assertFalse(result.cleanup_confirmed)
            self.assertIn(
                "descendant could not initialize the process guard",
                result.output,
            )

    @unittest.skipUnless(os.name == "posix", "POSIX descendant containment")
    def test_uncooperative_detached_child_fails_closed(self) -> None:
        helper_source = textwrap.dedent(
            """
            from pathlib import Path
            import os
            import subprocess
            import sys
            from process_guard.authorization import allow_detached_process

            with allow_detached_process():
                child = subprocess.Popen(
                    ["/bin/sleep", "60"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            Path(sys.argv[1]).write_text(
                f"{child.pid}\\n",
                encoding="utf-8",
            )
            os._exit(0)
            """
        )

        with TemporaryDirectory(
            prefix="test-runner-uncooperative-"
        ) as temporary:
            root = Path(temporary)
            helper = root / "uncooperative_helper.py"
            pid_path = root / "child.pid"
            helper.write_text(helper_source, encoding="utf-8")
            child_pid: int | None = None
            try:
                with patch.object(
                    run_tests,
                    "test_command",
                    return_value=[
                        sys.executable,
                        str(helper),
                        str(pid_path),
                    ],
                ), patch.object(
                    run_tests,
                    "DESCENDANT_CLEANUP_TIMEOUT_SECONDS",
                    0.2,
                ):
                    result = run_tests.run_module(
                        "tests.uncooperative_helper",
                        5,
                        run_tests.ProcessRegistry(),
                    )

                child_pid = int(pid_path.read_text(encoding="utf-8"))
                self.assertFalse(result.passed)
                self.assertFalse(result.cleanup_confirmed)
                self.assertIn(
                    "escaped or untracked descendant",
                    result.output,
                )
                os.kill(child_pid, 0)
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_cleanup_uncertainty_is_a_failure_and_is_recorded(self) -> None:
        registry = run_tests.ProcessRegistry()
        stopped = SimpleNamespace(
            stdout="partial stdout\n",
            stderr="partial stderr\n",
            diagnostic=(
                "The process-group leader was already reaped; cleanup "
                "cannot be verified without risking a reused "
                "process-group ID."
            ),
            cleanup_confirmed=False,
            leader_kill_sent=False,
            permission_denied_after_anchored_exit=False,
        )

        with patch.object(
            run_tests,
            "_stop_process_group",
            return_value=stopped,
        ) as stop_process_group:
            result = run_tests._stop_module_process(
                "tests.test_stale_leader",
                object(),
                registry,
                timed_out=False,
                reason="test-runner interruption",
            )

        stop_process_group.assert_called_once()
        self.assertFalse(result.passed)
        self.assertFalse(result.cleanup_confirmed)
        self.assertEqual(1, result.returncode)
        self.assertIn("already reaped", result.output)
        self.assertEqual(
            (
                "tests.test_stale_leader: "
                f"{stopped.diagnostic}",
            ),
            registry.cleanup_uncertainties(),
        )

    def test_permission_denied_exit_needs_guarded_descendant_confirmation(
        self,
    ) -> None:
        stopped = SimpleNamespace(
            stdout="",
            stderr="",
            diagnostic="Could not confirm process-group termination.",
            cleanup_confirmed=False,
            leader_kill_sent=False,
            permission_denied_after_anchored_exit=True,
        )

        for guard_required, expected_cleanup in ((False, False), (True, True)):
            with self.subTest(guard_required=guard_required):
                registry = run_tests.ProcessRegistry()
                process = SimpleNamespace(
                    returncode=0,
                    _test_guard_required=guard_required,
                )
                with patch.object(
                    run_tests,
                    "_stop_process_group",
                    return_value=stopped,
                ), patch.object(
                    run_tests,
                    "_request_descendant_stop",
                    return_value=(True, None),
                ), patch.object(
                    run_tests,
                    "_confirm_descendant_cleanup",
                    return_value=(True, None),
                ):
                    result = run_tests._stop_module_process(
                        "tests.test_eperm",
                        process,
                        registry,
                        timed_out=False,
                        reason="normal module completion",
                        completed=True,
                    )

                self.assertEqual(expected_cleanup, result.cleanup_confirmed)
                self.assertEqual(expected_cleanup, result.passed)
                self.assertEqual(
                    () if expected_cleanup else (
                        "tests.test_eperm: "
                        "Could not confirm process-group termination.",
                    ),
                    registry.cleanup_uncertainties(),
                )

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "waitpid"),
        "requires POSIX child-process ownership",
    )
    def test_reaped_leader_is_never_signaled_by_runner_cleanup(self) -> None:
        with allow_detached_process():
            process = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        os.waitpid(process.pid, 0)
        registry = run_tests.ProcessRegistry()
        try:
            with patch.object(libskillpack.os, "killpg") as killpg:
                result = run_tests._stop_module_process(
                    "tests.test_reaped",
                    process,
                    registry,
                    timed_out=False,
                    reason="test-runner interruption",
                )

            killpg.assert_not_called()
            self.assertFalse(result.passed)
            self.assertFalse(result.cleanup_confirmed)
            self.assertIn("already reaped", result.output)
            self.assertTrue(registry.cleanup_uncertainties())
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    def test_interruption_terminates_and_reaps_active_module(self) -> None:
        with TemporaryDirectory(prefix="test-runner-interrupt-") as temporary:
            root = Path(temporary)
            pid_path = root / "pid.txt"
            helper = root / "interrupt_helper.py"
            helper.write_text(
                textwrap.dedent(
                    """
                    from pathlib import Path
                    import os
                    import sys
                    import time

                    Path(sys.argv[1]).write_text(
                        f"{os.getpid()}\\n",
                        encoding="utf-8",
                    )
                    time.sleep(60)
                    """
                ),
                encoding="utf-8",
            )
            original_run_module = run_tests.run_module

            def worker(
                module: str,
                timeout_seconds: float,
                registry: run_tests.ProcessRegistry,
            ) -> run_tests.ModuleResult:
                if module == "tests.test_z_interrupt":
                    deadline = time.monotonic() + 2
                    while (
                        not pid_path.is_file()
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    raise KeyboardInterrupt
                return original_run_module(
                    module,
                    timeout_seconds,
                    registry,
                )

            with patch.object(
                run_tests,
                "test_command",
                return_value=[
                    sys.executable,
                    str(helper),
                    str(pid_path),
                ],
            ), patch.object(
                run_tests,
                "run_module",
                side_effect=worker,
            ), self.assertRaises(KeyboardInterrupt):
                run_tests.run_modules(
                    [
                        "tests.test_a_sleeper",
                        "tests.test_z_interrupt",
                    ],
                    jobs=2,
                    timeout_seconds=30,
                    global_timeout_seconds=30,
                    registry=run_tests.ProcessRegistry(),
                )

            process_id = int(pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(process_id, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail(
                    f"interrupted test process {process_id} still exists"
                )


if __name__ == "__main__":
    unittest.main()
