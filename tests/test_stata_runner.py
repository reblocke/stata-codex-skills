from __future__ import annotations

from contextlib import chdir
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import stata_runner  # noqa: E402
from process_guard.authorization import allow_detached_process  # noqa: E402


FAKE_STATA = r'''
import json
import os
from pathlib import Path
import re
import sys
import time

wrapper = Path(sys.argv[-1])
text = wrapper.read_text()
target = Path(re.search(r'^capture noisily do "([^"]+)"$', text, re.M).group(1))
sidecar = Path(re.search(r'^file open .+ using "([^"]+)", write text$', text, re.M).group(1))
token = re.search(r'STATA_RUNNER::([0-9a-f]{32})::', text).group(1)
mode = target.read_text().strip()
Path('invocation.json').write_text(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd(), 'pwd': os.environ.get('PWD')}))
Path('stata.log').write_text('Analytical data retained only in the private log.\n' + text)
print('Analytical stdout retained only in its private file.')
print('Analytical stderr retained only in its private file.', file=sys.stderr)
if mode == 'missing':
    sys.exit(0)
if mode == 'fifo':
    os.mkfifo(sidecar)
    sys.exit(0)
if mode == 'stale':
    token = 'stale-token'
if mode == 'malformed':
    sidecar.write_text(f'STATA_RUNNER::{token}::0\nextra\n')
    sys.exit(0)
rc = 199 if mode == 'stata-error' else 0
sidecar.write_text(f'STATA_RUNNER::{token}::{rc}\n')
if mode == 'missing-log':
    Path('stata.log').unlink()
if mode == 'timeout':
    time.sleep(60)
sys.exit(9 if mode == 'os-error' else 0)
'''


class LaunchArgumentsTests(unittest.TestCase):
    def test_darwin_app_bundle_uses_unattended_wrapper_argument(self) -> None:
        for edition in ("StataBE", "StataSE", "StataMP"):
            binary = Path(f"/Applications/Stata/{edition}.app/Contents/MacOS/{edition}")
            with self.subTest(edition=edition):
                self.assertEqual(
                    [str(binary), "-e", "wrapper with spaces.do"],
                    stata_runner.launch_arguments(binary, Path("wrapper with spaces.do"), "Darwin"),
                )

    def test_console_executables_keep_batch_do_arguments(self) -> None:
        for system in ("Darwin", "Linux"):
            with self.subTest(system=system):
                self.assertEqual(
                    ["/opt/stata/stata-mp", "-b", "do", "wrapper.do"],
                    stata_runner.launch_arguments(Path("/opt/stata/stata-mp"), Path("wrapper.do"), system),
                )

    def test_resolved_symlink_to_app_uses_app_interface(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            binary = root / "StataBE.app/Contents/MacOS/StataBE"
            binary.parent.mkdir(parents=True)
            binary.touch()
            link = root / "stata"
            link.symlink_to(binary)
            self.assertEqual(
                [str(binary), "-e", "wrapper.do"],
                stata_runner.launch_arguments(link, Path("wrapper.do"), "Darwin"),
            )

    def test_windows_fails_without_a_guessed_launcher(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported on Windows"):
            stata_runner.launch_arguments(Path("Stata.exe"), Path("wrapper.do"), "Windows")


class RunStataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(allow_detached_process())
        self.temporary = TemporaryDirectory(prefix="stata runner tests ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.binary = self.root / "fake Stata"
        self.binary.write_text(f"#!{Path(sys.executable).resolve()}\n" + FAKE_STATA)
        self.binary.chmod(0o700)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.target = self.project / "analysis with spaces.do"
        self.target.write_text("success")
        self.run_dir = self.root / "retained run"

    def cli_arguments(self) -> list[str]:
        # Authorize reviewed fixture sessions when the repository guard is active;
        # the distributed helper has no dependency on this test-only guard.
        bootstrap = (
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT / 'scripts')!r})\n"
            "from process_guard.authorization import allow_detached_process\n"
            "sys.argv = sys.argv[1:]\n"
            "with allow_detached_process():\n"
            "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
        )
        return [
            sys.executable, "-c", bootstrap,
            str(REPO_ROOT / "scripts/stata_runner.py"),
            "--stata", str(self.binary), "--do-file", str(self.target),
            "--run-dir", str(self.run_dir),
        ]

    def run_fake(self, mode: str, *, timeout: float = 3) -> dict[str, object]:
        self.target.write_text(mode)
        return stata_runner.run_stata(
            self.binary, self.target, self.run_dir,
            cwd=self.project, timeout_seconds=timeout,
        )

    def test_success_requires_natural_exit_and_both_return_codes(self) -> None:
        result = self.run_fake("success")
        self.assertTrue(result["success"])
        self.assertTrue(result["natural_exit"])
        self.assertFalse(result["timed_out"])
        self.assertEqual(0, result["os_returncode"])
        self.assertEqual(0, result["stata_returncode"])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(result, json.loads((self.run_dir / "result.json").read_text()))
        invocation = json.loads((self.run_dir / "invocation.json").read_text())
        self.assertEqual(str(self.run_dir), invocation["cwd"])
        self.assertEqual(str(self.run_dir / "wrapper.do"), invocation["args"][-1])
        wrapper = (self.run_dir / "wrapper.do").read_text()
        self.assertIn(f'cd "{self.project}"\n', wrapper)
        self.assertIn(
            f'capture noisily do "{self.target}"\nlocal codex_stata_rc = _rc\n',
            wrapper,
        )
        self.assertIn("exit `codex_stata_rc', clear STATA\n", wrapper)
        self.assertEqual(0o700, stat.S_IMODE(self.run_dir.stat().st_mode))
        for artifact in self.run_dir.iterdir():
            self.assertEqual(0, stat.S_IMODE(artifact.stat().st_mode) & 0o077)

    def test_stata_failure_with_os_zero_is_failure(self) -> None:
        result = self.run_fake("stata-error")
        self.assertFalse(result["success"])
        self.assertTrue(result["natural_exit"])
        self.assertEqual(0, result["os_returncode"])
        self.assertEqual(199, result["stata_returncode"])
        self.assertEqual(1, result["exit_code"])

    def test_os_failure_with_stata_zero_is_failure(self) -> None:
        result = self.run_fake("os-error")
        self.assertFalse(result["success"])
        self.assertEqual(9, result["os_returncode"])
        self.assertEqual(0, result["stata_returncode"])
        self.assertEqual(1, result["exit_code"])

    def test_echoed_wrapper_log_is_not_completion_evidence(self) -> None:
        result = self.run_fake("missing")
        self.assertIn("STATA_RUNNER::", (self.run_dir / "stata.log").read_text())
        self.assertFalse(result["success"])
        self.assertIsNone(result["stata_returncode"])
        self.assertEqual(1, result["exit_code"])

    def test_stale_and_malformed_sidecars_fail(self) -> None:
        for mode in ("stale", "malformed"):
            with self.subTest(mode=mode):
                self.run_dir = self.root / mode
                result = self.run_fake(mode)
                self.assertFalse(result["success"])
                self.assertIsNone(result["stata_returncode"])
                self.assertEqual(1, result["exit_code"])

    def test_fifo_sidecar_fails_without_waiting_for_a_writer(self) -> None:
        self.target.write_text("fifo")
        completed = subprocess.run(
            [*self.cli_arguments(), "--timeout", "1"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        self.assertEqual(1, completed.returncode)
        self.assertIsNone(json.loads(completed.stdout)["stata_returncode"])

    def test_missing_retained_log_is_failure_despite_both_zero_statuses(self) -> None:
        result = self.run_fake("missing-log")
        self.assertFalse(result["success"])
        self.assertEqual(0, result["os_returncode"])
        self.assertEqual(0, result["stata_returncode"])
        self.assertEqual(1, result["exit_code"])

    def test_completion_sidecar_does_not_allow_a_hung_process_to_pass(self) -> None:
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            result = self.run_fake("timeout", timeout=2)
            self.assertFalse(result["success"])
            self.assertFalse(result["natural_exit"])
            self.assertTrue(result["timed_out"])
            self.assertTrue(result["cleanup_succeeded"])
            self.assertEqual(0, result["stata_returncode"])
            self.assertEqual(124, result["exit_code"])
            self.assertIn(result["os_returncode"], (-signal.SIGTERM, -signal.SIGKILL))
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_default_cwd_is_callers_project_directory(self) -> None:
        with chdir(self.project):
            result = stata_runner.run_stata(self.binary, self.target, self.run_dir)
        self.assertTrue(result["success"])
        self.assertEqual(str(self.project), result["cwd"])

    def test_child_pwd_and_cwd_both_point_to_private_run_directory(self) -> None:
        with patch.dict(os.environ, {"PWD": str(self.project)}):
            result = self.run_fake("success")
            self.assertEqual(str(self.project), os.environ["PWD"])
        self.assertTrue(result["success"])
        invocation = json.loads((self.run_dir / "invocation.json").read_text())
        self.assertEqual(str(self.run_dir), invocation["cwd"])
        self.assertEqual(str(self.run_dir), invocation["pwd"])

    def test_existing_run_directory_is_never_overwritten(self) -> None:
        self.run_dir.mkdir()
        sentinel = self.run_dir / "wrapper.do"
        sentinel.write_text("prior evidence")
        with self.assertRaises(FileExistsError):
            self.run_fake("success")
        self.assertEqual("prior evidence", sentinel.read_text())
        self.assertEqual([sentinel], list(self.run_dir.iterdir()))

    def test_dangling_run_directory_symlink_is_not_followed(self) -> None:
        absent = self.root / "absent destination"
        self.run_dir.symlink_to(absent, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            self.run_fake("success")
        self.assertFalse(absent.exists())

    def test_unsafe_stata_path_characters_fail_before_launch(self) -> None:
        for character in ('"', "'", "`", "$", "\n", "\r"):
            with self.subTest(character=character):
                target = self.project / f"unsafe{character}.do"
                target.write_text("success")
                with self.assertRaisesRegex(ValueError, "Stata paths cannot"):
                    stata_runner.run_stata(self.binary, target, self.run_dir)
                self.assertFalse(self.run_dir.exists())

    def test_invalid_timeouts_fail_before_creating_artifacts(self) -> None:
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.run_fake("success", timeout=timeout)
        self.assertFalse(self.run_dir.exists())

    def test_cli_emits_only_structured_execution_metadata(self) -> None:
        completed = subprocess.run(
            [*self.cli_arguments(), "--cwd", str(self.project), "--timeout", "3"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["success"])
        self.assertNotIn("Analytical", completed.stdout + completed.stderr)
        self.assertIn("Analytical stdout", (self.run_dir / "stdout.txt").read_text())
        self.assertIn("Analytical stderr", (self.run_dir / "stderr.txt").read_text())

    def test_cli_invalid_invocation_returns_two_without_replacing_evidence(self) -> None:
        self.run_dir.mkdir()
        completed = subprocess.run(
            self.cli_arguments(),
            capture_output=True, text=True, timeout=5, check=False,
        )
        self.assertEqual(2, completed.returncode)
        self.assertEqual(2, json.loads(completed.stdout)["exit_code"])
        self.assertEqual([], list(self.run_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
