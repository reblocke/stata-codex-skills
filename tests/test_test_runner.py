from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import time
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_tests  # noqa: E402
import libskillpack  # noqa: E402
from process_guard.authorization import allow_detached_process  # noqa: E402


class TestRunnerTests(unittest.TestCase):


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
