from __future__ import annotations

from contextlib import chdir
import os
from pathlib import Path
import signal
from subprocess import CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
import sys
import time
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import libskillpack  # noqa: E402


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def read(self) -> bytes:
        return self.data


class ImmediateProcess:
    def __init__(self, returncode: int = 0) -> None:
        self.args = ["stata"]
        self.pid = 999_999_991
        self.returncode = returncode
        self.terminate_called = False

    def poll(self) -> int:
        return self.returncode

    def communicate(self, timeout: int | float | None = None) -> tuple[str, str]:
        del timeout
        return "", ""

    def terminate(self) -> None:
        self.terminate_called = True


class HangingProcess:
    def __init__(self) -> None:
        self.args = ["stata"]
        self.pid = 999_999_992
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False
        self.communicate_calls = 0

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True

    def communicate(self, timeout: int | float | None = None) -> tuple[str, str]:
        self.communicate_calls += 1
        if not self.kill_called:
            raise TimeoutExpired(self.args, timeout)
        self.returncode = -9
        return "", ""


class MarkerThenNonexitProcess:
    def __init__(self) -> None:
        self.args = ["stata"]
        self.pid = 999_999_993
        self.returncode: int | None = None
        self.terminate_called = False
        self.kill_called = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminate_called = True
        self.returncode = -15

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9

    def communicate(
        self, timeout: int | float | None = None
    ) -> tuple[str, str]:
        del timeout
        return "", ""


class RunStataDoTests(unittest.TestCase):
    def setUp(self) -> None:
        killpg_patcher = patch.object(
            libskillpack.os,
            "killpg",
            side_effect=ProcessLookupError,
        )
        self.killpg = killpg_patcher.start()
        self.addCleanup(killpg_patcher.stop)

    def _make_do_file(self, root: Path, name: str = "smoke.do") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        do_file = root / name
        do_file.write_text("clear all\n", encoding="utf-8")
        return do_file

    def test_exact_fresh_log_and_marker_succeed(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)
            marker = "VALIDATION COMPLETE: fresh-run-id"

            def fake_popen(*args, **kwargs) -> ImmediateProcess:
                del args, kwargs
                (cwd / "smoke.log").write_text(f"{marker}\n", encoding="utf-8")
                return ImmediateProcess()

            with patch.object(libskillpack.subprocess, "Popen", side_effect=fake_popen):
                result, log_path = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=1,
                )

            self.assertEqual(0, result.returncode)
            self.assertEqual(cwd / "smoke.log", log_path)
            self.assertEqual(f"{marker}\n", log_path.read_text(encoding="utf-8"))

    def test_popen_receives_matching_cwd_and_pwd_environment(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)
            marker = "VALIDATION COMPLETE: cwd-contract"

            def fake_popen(*args, **kwargs) -> ImmediateProcess:
                del args
                self.assertEqual(str(cwd), kwargs["cwd"])
                self.assertEqual(str(cwd), kwargs["env"]["PWD"])
                self.assertTrue(kwargs["start_new_session"])
                (cwd / "smoke.log").write_text(f"{marker}\n", encoding="utf-8")
                return ImmediateProcess()

            with patch.object(libskillpack.subprocess, "Popen", side_effect=fake_popen):
                result, _ = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=1,
                )

            self.assertEqual(0, result.returncode)

    def test_relative_do_file_exists_from_child_workdir(self) -> None:
        with TemporaryDirectory(prefix="stata-run-relative-") as temp_root:
            root = Path(temp_root)
            cwd = Path("work")
            marker = "VALIDATION COMPLETE: child-cwd"
            stub = root / "stata-stub"
            stub.write_text(
                "#!/bin/sh\n"
                'do_file="$3"\n'
                '[ -f "$do_file" ] || exit 9\n'
                'log_file="${do_file%.do}.log"\n'
                f"printf '%s\\n' '{marker}' > \"$log_file\"\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            with chdir(root):
                do_file = self._make_do_file(cwd)
                result, log_path = libskillpack.run_stata_do(
                    stub,
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=2,
                )

            self.assertEqual(0, result.returncode)
            self.assertEqual(root / "work" / "smoke.log", root / log_path)

    def test_preexisting_workdir_log_is_not_accepted_as_fresh(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)
            marker = "VALIDATION COMPLETE: current-run"
            stale_log = cwd / "smoke.log"
            stale_log.write_text(f"{marker}\n", encoding="utf-8")

            with patch.object(
                libskillpack.subprocess, "Popen", return_value=ImmediateProcess()
            ):
                result, log_path = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=1,
                )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(cwd / "smoke.log", log_path)
            self.assertFalse(log_path.exists())

    def test_stale_repo_root_log_is_ignored(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            temp_root_path = Path(temp_root)
            fake_repo = temp_root_path / "repo"
            cwd = temp_root_path / "work"
            do_file = self._make_do_file(cwd)
            fake_repo.mkdir()
            marker = "VALIDATION COMPLETE: current-run"
            stale_root_log = fake_repo / "smoke.log"
            stale_root_log.write_text(f"{marker}\n", encoding="utf-8")

            with patch.object(libskillpack, "REPO_ROOT", fake_repo), patch.object(
                libskillpack.subprocess, "Popen", return_value=ImmediateProcess()
            ):
                result, log_path = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=1,
                )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(cwd / "smoke.log", log_path)
            self.assertEqual(
                f"{marker}\n", stale_root_log.read_text(encoding="utf-8")
            )
            self.assertEqual(["smoke.log"], [path.name for path in fake_repo.iterdir()])

    def test_log_beside_do_file_outside_cwd_is_ignored(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            temp_root_path = Path(temp_root)
            source_dir = temp_root_path / "source"
            cwd = temp_root_path / "work"
            do_file = self._make_do_file(source_dir)
            cwd.mkdir()
            marker = "VALIDATION COMPLETE: current-run"
            adjacent_log = source_dir / "smoke.log"
            adjacent_log.write_text(f"{marker}\n", encoding="utf-8")

            with patch.object(
                libskillpack.subprocess, "Popen", return_value=ImmediateProcess()
            ):
                result, log_path = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=1,
                )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(cwd / "smoke.log", log_path)
            self.assertFalse(log_path.exists())
            self.assertEqual(
                f"{marker}\n", adjacent_log.read_text(encoding="utf-8")
            )

    def test_zero_return_without_marker_fails(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)

            def fake_popen(*args, **kwargs) -> ImmediateProcess:
                del args, kwargs
                (cwd / "smoke.log").write_text("normal Stata output\n", encoding="utf-8")
                return ImmediateProcess(returncode=0)

            with patch.object(libskillpack.subprocess, "Popen", side_effect=fake_popen):
                result, _ = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker="VALIDATION COMPLETE: expected",
                    timeout_seconds=1,
                )

            self.assertNotEqual(0, result.returncode)

    def test_exact_marker_stops_nonexiting_process_and_succeeds(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)
            marker = "VALIDATION COMPLETE: current-run"
            process = MarkerThenNonexitProcess()

            def fake_popen(*args, **kwargs) -> MarkerThenNonexitProcess:
                del args, kwargs
                (cwd / "smoke.log").write_text(f"{marker}\n", encoding="utf-8")
                return process

            def signal_group(_pid: int, sent_signal: int) -> None:
                if sent_signal == signal.SIGTERM:
                    process.terminate()
                elif process.returncode is None:
                    process.kill()
                else:
                    raise ProcessLookupError

            self.killpg.side_effect = signal_group
            with patch.object(libskillpack.subprocess, "Popen", side_effect=fake_popen):
                result, _ = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=1,
                )

            self.assertEqual(0, result.returncode)
            self.assertTrue(process.terminate_called)
            self.assertFalse(process.kill_called)

    def test_wrong_marker_fails(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)

            def fake_popen(*args, **kwargs) -> ImmediateProcess:
                del args, kwargs
                (cwd / "smoke.log").write_text(
                    "VALIDATION COMPLETE: other-run\n", encoding="utf-8"
                )
                return ImmediateProcess(returncode=0)

            with patch.object(libskillpack.subprocess, "Popen", side_effect=fake_popen):
                result, _ = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker="VALIDATION COMPLETE: expected",
                    timeout_seconds=1,
                )

            self.assertNotEqual(0, result.returncode)

    def test_timeout_terminates_then_kills_process_and_returns_124(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)
            process = HangingProcess()

            self.killpg.side_effect = (
                lambda _pid, sent_signal: process.terminate()
                if sent_signal == signal.SIGTERM
                else process.kill()
            )
            with patch.object(libskillpack.subprocess, "Popen", return_value=process):
                result, log_path = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker="VALIDATION COMPLETE: expected",
                    timeout_seconds=0,
                )

            self.assertEqual(124, result.returncode)
            self.assertTrue(process.terminate_called)
            self.assertTrue(process.kill_called)
            self.assertEqual(2, process.communicate_calls)
            self.assertEqual(cwd / "smoke.log", log_path)


class RunStataDescendantCleanupTests(unittest.TestCase):
    def _assert_process_gone(self, pid: int) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        self.fail(f"descendant process {pid} survived Stata cleanup")

    def _run_stub(self, *, write_marker: bool, timeout_seconds: float) -> int:
        with TemporaryDirectory(prefix="stata-descendant-") as temp_root:
            cwd = Path(temp_root) / "work"
            cwd.mkdir()
            do_file = cwd / "smoke.do"
            do_file.write_text("clear all\n", encoding="utf-8")
            marker = "VALIDATION COMPLETE: descendant-cleanup"
            marker_command = (
                f"printf '%s\\n' '{marker}' > \"$log_file\"\n"
                if write_marker
                else ""
            )
            stub = Path(temp_root) / "stata-stub"
            stub.write_text(
                "#!/bin/sh\n"
                'do_file="$3"\n'
                'log_file="${do_file%.do}.log"\n'
                "(trap '' TERM; exec sleep 30) >/dev/null 2>&1 &\n"
                "child_pid=$!\n"
                "printf '%s\\n' \"$child_pid\" > child.pid\n"
                f"{marker_command}"
                'wait "$child_pid"\n',
                encoding="utf-8",
            )
            stub.chmod(0o755)
            result, _ = libskillpack.run_stata_do(
                stub,
                do_file,
                cwd,
                completion_marker=marker,
                timeout_seconds=timeout_seconds,
            )
            child_pid = int((cwd / "child.pid").read_text(encoding="utf-8"))
            self._assert_process_gone(child_pid)
            return result.returncode

    @unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
    def test_marker_completion_removes_descendants(self) -> None:
        self.assertEqual(0, self._run_stub(write_marker=True, timeout_seconds=2))

    @unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
    def test_timeout_removes_descendants(self) -> None:
        self.assertEqual(124, self._run_stub(write_marker=False, timeout_seconds=1))


class StrictYamlTests(unittest.TestCase):
    def test_read_yaml_rejects_duplicate_top_level_key_with_source_lines(self) -> None:
        with TemporaryDirectory(prefix="strict-yaml-") as temp_root:
            path = Path(temp_root) / "duplicate.yaml"
            path.write_text("slug: first\nslug: second\n", encoding="utf-8")

            with self.assertRaises(libskillpack.yaml.YAMLError) as caught:
                libskillpack.read_yaml(path)

            message = str(caught.exception)
            self.assertIn(str(path), message)
            self.assertIn("duplicate key 'slug'", message)
            self.assertIn("first occurrence was at line 1, column 1", message)
            self.assertIn("line 2, column 1", message)

    def test_parse_yaml_rejects_nested_duplicate_key_from_bytes(self) -> None:
        source = Path("/reviewed/content.yaml")
        payload = b"outer:\n  nested:\n    value: first\n    value: second\n"

        with self.assertRaises(libskillpack.yaml.YAMLError) as caught:
            libskillpack.parse_yaml(payload, source=source)

        message = str(caught.exception)
        self.assertIn(str(source), message)
        self.assertIn("duplicate key 'value'", message)
        self.assertIn("first occurrence was at line 3, column 5", message)
        self.assertIn("line 4, column 5", message)


class ErrorDetectionTests(unittest.TestCase):
    def test_stata_error_allows_leading_whitespace(self) -> None:
        self.assertTrue(libskillpack.has_stata_error("output\n    r(198);\n"))

    def test_stata_error_does_not_match_prose(self) -> None:
        self.assertFalse(libskillpack.has_stata_error("The text mentions r(198); inline."))


class TimeoutAndChecksumTests(unittest.TestCase):
    def test_run_command_converts_timeout_to_return_code_124(self) -> None:
        timeout = TimeoutExpired(
            ["fake-command"],
            7,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )
        with patch.object(libskillpack.subprocess, "run", side_effect=timeout):
            result = libskillpack.run_command(
                ["fake-command"],
                cwd=Path("/tmp"),
                timeout_seconds=7,
            )

        self.assertIsInstance(result, CompletedProcess)
        self.assertEqual(124, result.returncode)
        self.assertEqual("partial stdout", result.stdout)
        self.assertIn("partial stderr", result.stderr)
        self.assertIn("timed out after 7 seconds", result.stderr)

    def test_download_binary_rejects_checksum_mismatch_without_writing(self) -> None:
        with TemporaryDirectory(prefix="download-test-") as temp_root:
            destination = Path(temp_root) / "sdk.c"
            with patch.object(
                libskillpack.urllib.request,
                "urlopen",
                return_value=FakeResponse(b"unexpected bytes"),
            ):
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    libskillpack.download_binary(
                        "https://example.invalid/sdk.c",
                        destination,
                        timeout_seconds=9,
                        expected_sha256="0" * 64,
                    )

            self.assertFalse(destination.exists())

    def test_download_binary_passes_timeout_and_leaves_no_partial_file(self) -> None:
        with TemporaryDirectory(prefix="download-test-") as temp_root:
            destination = Path(temp_root) / "sdk.c"
            with patch.object(
                libskillpack.urllib.request,
                "urlopen",
                side_effect=TimeoutError("network timeout"),
            ) as urlopen:
                with self.assertRaisesRegex(TimeoutError, "network timeout"):
                    libskillpack.download_binary(
                        "https://example.invalid/sdk.c",
                        destination,
                        timeout_seconds=11,
                    )

            self.assertEqual(11, urlopen.call_args.kwargs["timeout"])
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
