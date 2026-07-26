from __future__ import annotations

from contextlib import chdir
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from subprocess import CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
import sys
import time
import unittest
from unittest.mock import call, patch


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

    def poll(self) -> int | None:
        return self.returncode

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


class InterruptingPipe:
    def __init__(self, *, interrupt: bool = False) -> None:
        self.interrupt = interrupt
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self.interrupt:
            raise KeyboardInterrupt("pipe close")


class PollingFailureProcess:
    def __init__(self) -> None:
        self.args = ["stata"]
        self.pid = 999_999_994
        self.returncode: int | None = None
        self.stdout = InterruptingPipe(interrupt=True)
        self.stderr = InterruptingPipe()
        self.wait_called = False

    def poll(self) -> None:
        return None

    def wait(self, timeout: int | float | None = None) -> int:
        del timeout
        self.wait_called = True
        self.returncode = -9
        return self.returncode


class RunStataDoTests(unittest.TestCase):
    def setUp(self) -> None:
        launcher_patcher = patch.object(
            libskillpack,
            "_stata_launch_command",
            side_effect=lambda binary, do_file: [
                str(binary),
                "-e",
                "do",
                str(do_file),
            ],
        )
        launcher_patcher.start()
        self.addCleanup(launcher_patcher.stop)
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
            self.assertEqual(
                [call(process.pid, signal.SIGTERM)],
                self.killpg.call_args_list,
            )

    def test_already_reaped_process_group_is_never_signaled(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)
            marker = "VALIDATION COMPLETE: already-reaped"

            def fake_popen(*args, **kwargs) -> ImmediateProcess:
                del args, kwargs
                (cwd / "smoke.log").write_text(f"{marker}\n", encoding="utf-8")
                return ImmediateProcess(returncode=0)

            with patch.object(
                libskillpack.subprocess,
                "Popen",
                side_effect=fake_popen,
            ):
                result, _ = libskillpack.run_stata_do(
                    Path("/fake/stata"),
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=1,
                )

            self.assertEqual(0, result.returncode)
            self.killpg.assert_not_called()

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

    def test_polling_exception_survives_interrupting_pipe_cleanup(self) -> None:
        with TemporaryDirectory(prefix="stata-run-") as temp_root:
            cwd = Path(temp_root) / "work"
            do_file = self._make_do_file(cwd)
            process = PollingFailureProcess()
            original = KeyboardInterrupt("polling failure")

            def fake_popen(*args, **kwargs) -> PollingFailureProcess:
                del args, kwargs
                (cwd / "smoke.log").write_text("polling\n", encoding="utf-8")
                return process

            with patch.object(
                libskillpack.subprocess,
                "Popen",
                side_effect=fake_popen,
            ), patch.object(
                libskillpack,
                "read_text",
                side_effect=original,
            ):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    libskillpack.run_stata_do(
                        Path("/fake/stata"),
                        do_file,
                        cwd,
                        completion_marker="VALIDATION COMPLETE: expected",
                        timeout_seconds=1,
                    )

            self.assertIs(original, caught.exception)
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)
            self.assertTrue(process.wait_called)


class StataContainmentCommandTests(unittest.TestCase):
    def test_containment_probe_reports_nested_sandbox_failure(self) -> None:
        with patch.object(libskillpack.sys, "platform", "darwin"), patch.object(
            libskillpack,
            "MACOS_SANDBOX_EXEC",
            Path("/usr/bin/true"),
        ), patch.object(
            libskillpack.subprocess,
            "run",
            return_value=CompletedProcess(["sandbox-exec"], 1, "", "denied"),
        ):
            available, reason = libskillpack.stata_containment_status()

        self.assertFalse(available)
        self.assertIn("could not apply", reason)

    def test_unsupported_platform_fails_before_launch(self) -> None:
        with patch.object(libskillpack.sys, "platform", "linux"):
            with self.assertRaisesRegex(OSError, "requires macOS"):
                libskillpack._stata_launch_command(
                    Path("/fake/stata"),
                    Path("smoke.do"),
                )

    def test_launch_uses_fixed_fork_denying_profile(self) -> None:
        with patch.object(libskillpack.sys, "platform", "darwin"), patch.object(
            libskillpack,
            "MACOS_SANDBOX_EXEC",
            Path("/usr/bin/true"),
        ), patch.object(
            libskillpack,
            "stata_containment_status",
            return_value=(True, ""),
        ):
            command = libskillpack._stata_launch_command(
                Path("/Applications/Stata/StataBE"),
                Path("smoke.do"),
            )

        self.assertEqual("/usr/bin/true", command[0])
        self.assertEqual("-p", command[1])
        self.assertEqual(
            (
                "(version 1)(allow default)(deny process-fork)"
                "(deny lsopen)"
                "(deny appleevent-send)"
            ),
            command[2],
        )
        self.assertEqual(
            [
                "/Applications/Stata/StataBE",
                "-e",
                "do",
                "smoke.do",
            ],
            command[3:],
        )


def _macos_process_sandbox_available() -> bool:
    return libskillpack.stata_containment_status()[0]


@unittest.skipUnless(
    _macos_process_sandbox_available(),
    "requires usable macOS sandbox-exec containment",
)
class RunStataContainmentIntegrationTests(unittest.TestCase):
    def _make_run(self, temp_root: str, marker: str) -> tuple[Path, Path]:
        cwd = Path(temp_root) / "work"
        cwd.mkdir()
        do_file = cwd / "smoke.do"
        do_file.write_text("clear all\n", encoding="utf-8")
        return cwd, do_file

    def test_benign_marker_succeeds(self) -> None:
        with TemporaryDirectory(prefix="stata-contained-benign-") as temp_root:
            marker = "VALIDATION COMPLETE: contained-benign"
            cwd, do_file = self._make_run(temp_root, marker)
            stub = Path(temp_root) / "stata-stub"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                f"open('smoke.log', 'w').write('{marker}\\n')\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            result, _ = libskillpack.run_stata_do(
                stub,
                do_file,
                cwd,
                completion_marker=marker,
                timeout_seconds=2,
            )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_process_creation_is_denied_before_marker_success(self) -> None:
        with TemporaryDirectory(prefix="stata-contained-fork-") as temp_root:
            marker = "VALIDATION COMPLETE: fork-denied"
            cwd, do_file = self._make_run(temp_root, marker)
            stub = Path(temp_root) / "stata-stub"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess\n"
                "import sys\n"
                "import time\n"
                "try:\n"
                "    child = subprocess.Popen(\n"
                "        [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
                "        start_new_session=True,\n"
                "    )\n"
                "except OSError:\n"
                "    open('fork-denied', 'w').write('denied\\n')\n"
                "else:\n"
                "    open('escaped.pid', 'w').write(str(child.pid))\n"
                f"open('smoke.log', 'w').write('{marker}\\n')\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            escaped_pid: int | None = None
            try:
                result, _ = libskillpack.run_stata_do(
                    stub,
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=2,
                )
                if (cwd / "escaped.pid").exists():
                    escaped_pid = int(
                        (cwd / "escaped.pid").read_text(encoding="utf-8")
                    )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue((cwd / "fork-denied").is_file())
                self.assertIsNone(escaped_pid)
            finally:
                if escaped_pid is not None:
                    try:
                        os.kill(escaped_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_posix_spawn_is_denied_before_marker_success(self) -> None:
        with TemporaryDirectory(prefix="stata-contained-posix-spawn-") as temp_root:
            marker = "VALIDATION COMPLETE: posix-spawn-denied"
            cwd, do_file = self._make_run(temp_root, marker)
            stub = Path(temp_root) / "stata-stub"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import time\n"
                "try:\n"
                "    child_pid = os.posix_spawn('/usr/bin/sleep', ['sleep', '30'], {})\n"
                "except OSError:\n"
                "    open('posix-spawn-denied', 'w').write('denied\\n')\n"
                "else:\n"
                "    open('escaped.pid', 'w').write(str(child_pid))\n"
                f"open('smoke.log', 'w').write('{marker}\\n')\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            escaped_pid: int | None = None
            try:
                result, _ = libskillpack.run_stata_do(
                    stub,
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=2,
                )
                if (cwd / "escaped.pid").exists():
                    escaped_pid = int(
                        (cwd / "escaped.pid").read_text(encoding="utf-8")
                    )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue((cwd / "posix-spawn-denied").is_file())
                self.assertIsNone(escaped_pid)
            finally:
                if escaped_pid is not None:
                    try:
                        os.kill(escaped_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_launch_services_cannot_delegate_background_process(self) -> None:
        compiler = shutil.which("clang")
        if compiler is None:
            self.skipTest("requires clang")
        with TemporaryDirectory(prefix="stata-contained-launch-services-") as temp_root:
            root = Path(temp_root)
            app_root = root / "Probe.app"
            executable_dir = app_root / "Contents" / "MacOS"
            executable_dir.mkdir(parents=True)
            survivor_pid_path = root / "survivor.pid"
            sleeper_source = root / "sleeper.c"
            sleeper_source.write_text(
                "#include <stdio.h>\n"
                "#include <unistd.h>\n"
                "int main(void) {\n"
                f"  FILE *handle = fopen({json.dumps(str(survivor_pid_path))}, \"w\");\n"
                "  if (handle == NULL) return 2;\n"
                "  fprintf(handle, \"%d\\n\", getpid());\n"
                "  fclose(handle);\n"
                "  sleep(30);\n"
                "  return 0;\n"
                "}\n",
                encoding="utf-8",
            )
            launcher_source = root / "launcher.c"
            launcher_source.write_text(
                "#include <CoreServices/CoreServices.h>\n"
                "#include <string.h>\n"
                "#include <unistd.h>\n"
                "int main(void) {\n"
                f"  const char *path = {json.dumps(str(app_root))};\n"
                "  CFURLRef url = CFURLCreateFromFileSystemRepresentation(\n"
                "      NULL, (const UInt8 *)path, (CFIndex)strlen(path), true);\n"
                "  if (url == NULL) return 3;\n"
                "  OSStatus status = LSOpenCFURLRef(url, NULL);\n"
                "  CFRelease(url);\n"
                "  sleep(2);\n"
                "  return status == noErr ? 0 : 4;\n"
                "}\n",
                encoding="utf-8",
            )
            (app_root / "Contents" / "Info.plist").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<plist version="1.0"><dict>\n'
                "<key>CFBundleExecutable</key><string>Probe</string>\n"
                "<key>CFBundleIdentifier</key>"
                f"<string>local.codex.stata-sandbox-probe.{os.getpid()}."
                f"{time.time_ns()}</string>\n"
                "<key>CFBundleName</key><string>Probe</string>\n"
                "<key>CFBundlePackageType</key><string>APPL</string>\n"
                "<key>CFBundleVersion</key><string>1</string>\n"
                "<key>LSBackgroundOnly</key><true/>\n"
                "</dict></plist>\n",
                encoding="utf-8",
            )
            launcher = root / "launcher"
            for command in (
                [
                    compiler,
                    str(sleeper_source),
                    "-o",
                    str(executable_dir / "Probe"),
                ],
                [
                    compiler,
                    str(launcher_source),
                    "-framework",
                    "CoreServices",
                    "-o",
                    str(launcher),
                ],
            ):
                compiled = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(0, compiled.returncode, compiled.stderr)

            survivor_pid: int | None = None
            try:
                control = subprocess.run(
                    [str(launcher)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(0, control.returncode, control.stderr)
                self.assertTrue(
                    survivor_pid_path.is_file(),
                    "unsandboxed control did not launch the probe app",
                )
                survivor_pid = int(survivor_pid_path.read_text(encoding="utf-8"))
                os.kill(survivor_pid, signal.SIGKILL)
                survivor_pid = None
                survivor_pid_path.unlink()
                time.sleep(0.2)

                contained = subprocess.run(
                    [
                        str(libskillpack.MACOS_SANDBOX_EXEC),
                        "-p",
                        libskillpack.STATA_SANDBOX_PROFILE,
                        str(launcher),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(
                    0,
                    contained.returncode,
                    "LaunchServices unexpectedly accepted the contained request",
                )
                self.assertFalse(
                    survivor_pid_path.exists(),
                    "LaunchServices delegated a process outside containment",
                )
            finally:
                if survivor_pid_path.exists():
                    survivor_pid = int(
                        survivor_pid_path.read_text(encoding="utf-8")
                    )
                if survivor_pid is not None:
                    try:
                        os.kill(survivor_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_rapid_double_fork_is_denied_before_orphaning(self) -> None:
        with TemporaryDirectory(prefix="stata-contained-double-fork-") as temp_root:
            marker = "VALIDATION COMPLETE: double-fork-denied"
            cwd, do_file = self._make_run(temp_root, marker)
            stub = Path(temp_root) / "stata-stub"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import time\n"
                "try:\n"
                "    first = os.fork()\n"
                "except OSError:\n"
                "    open('double-fork-denied', 'w').write('denied\\n')\n"
                "else:\n"
                "    if first == 0:\n"
                "        os.setsid()\n"
                "        second = os.fork()\n"
                "        if second > 0:\n"
                "            os._exit(0)\n"
                "        open('orphan.pid', 'w').write(str(os.getpid()))\n"
                "        while True:\n"
                "            time.sleep(1)\n"
                f"open('smoke.log', 'w').write('{marker}\\n')\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            orphan_pid: int | None = None
            try:
                result, _ = libskillpack.run_stata_do(
                    stub,
                    do_file,
                    cwd,
                    completion_marker=marker,
                    timeout_seconds=2,
                )
                if (cwd / "orphan.pid").exists():
                    orphan_pid = int(
                        (cwd / "orphan.pid").read_text(encoding="utf-8")
                    )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertTrue((cwd / "double-fork-denied").is_file())
                self.assertIsNone(orphan_pid)
            finally:
                if orphan_pid is not None:
                    try:
                        os.kill(orphan_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_timeout_reaps_contained_leader(self) -> None:
        with TemporaryDirectory(prefix="stata-contained-timeout-") as temp_root:
            marker = "VALIDATION COMPLETE: never"
            cwd, do_file = self._make_run(temp_root, marker)
            stub = Path(temp_root) / "stata-stub"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)

            result, _ = libskillpack.run_stata_do(
                stub,
                do_file,
                cwd,
                completion_marker=marker,
                timeout_seconds=1,
            )

        self.assertEqual(124, result.returncode)


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
