from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
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

    def test_success_is_concise_and_returns_zero(self) -> None:
        results = [
            run_tests.ModuleResult("tests.test_zeta", 0, "zeta noise\n"),
            run_tests.ModuleResult("tests.test_alpha", 0, "alpha noise\n"),
        ]
        output = io.StringIO()
        errors = io.StringIO()

        with patch.dict(
            os.environ,
            {"TEST_JOBS": "4", "TEST_TIMEOUT": "12"},
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

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "waitpid"),
        "requires POSIX child-process ownership",
    )
    def test_reaped_leader_is_never_signaled_by_runner_cleanup(self) -> None:
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
