from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
from tempfile import TemporaryDirectory
import sys
import threading
import time
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import libskillpack  # noqa: E402
from process_guard.authorization import allow_detached_process  # noqa: E402


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0
        self.timeouts: list[float] = []

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def settimeout(self, timeout_seconds: float) -> None:
        self.timeouts.append(timeout_seconds)

    def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.data):
            return b""
        if size < 0:
            size = len(self.data) - self.offset
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)


class ImmediateProcess:
    def __init__(self, returncode: int = 0, *, reaped: bool = False) -> None:
        self.args = ["stata"]
        self.pid = 999_999_991
        self.exit_code = returncode
        self.exited = True
        self.returncode: int | None = returncode if reaped else None
        self.terminate_called = False

    def poll(self) -> int:
        self.returncode = self.exit_code
        return self.exit_code

    def communicate(self, timeout: int | float | None = None) -> tuple[str, str]:
        del timeout
        self.returncode = self.exit_code
        return "", ""

    def terminate(self) -> None:
        self.terminate_called = True


class RunStataDoTests(unittest.TestCase):
    def setUp(self) -> None:
        launcher_patcher = patch.object(
            libskillpack,
            "_stata_launch_command",
            side_effect=lambda binary, do_file: [
                str(binary),
                "-b",
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
        real_state_observer = libskillpack._process_leader_state

        def observe_state(
            process: subprocess.Popen[str],
        ) -> libskillpack._ProcessLeaderState:
            if process.returncode is not None:
                return libskillpack._ProcessLeaderState.UNANCHORED
            if hasattr(process, "exited"):
                return (
                    libskillpack._ProcessLeaderState.EXITED_ANCHORED
                    if process.exited
                    else libskillpack._ProcessLeaderState.LIVE_ANCHORED
                )
            return real_state_observer(process)

        exit_observer_patcher = patch.object(
            libskillpack,
            "_process_leader_state",
            side_effect=observe_state,
        )
        exit_observer_patcher.start()
        self.addCleanup(exit_observer_patcher.stop)

    def _make_do_file(self, root: Path, name: str = "smoke.do") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        do_file = root / name
        do_file.write_text("clear all\n", encoding="utf-8")
        return do_file


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


    def test_exit_before_cleanup_after_marker_is_not_suppressed(self) -> None:
        for natural_returncode in (7, -int(signal.SIGKILL)):
            with self.subTest(returncode=natural_returncode), TemporaryDirectory(
                prefix="stata-run-"
            ) as temp_root:
                cwd = Path(temp_root) / "work"
                do_file = self._make_do_file(cwd)
                marker = "VALIDATION COMPLETE: natural-race"
                process = ImmediateProcess(returncode=natural_returncode)

                def fake_popen(*args, **kwargs) -> ImmediateProcess:
                    del args, kwargs
                    (cwd / "smoke.log").write_text(
                        f"{marker}\n",
                        encoding="utf-8",
                    )
                    return process

                states = iter([libskillpack._ProcessLeaderState.LIVE_ANCHORED])
                with patch.object(
                    libskillpack.subprocess,
                    "Popen",
                    side_effect=fake_popen,
                ), patch.object(
                    libskillpack,
                    "_process_leader_state",
                    side_effect=lambda _process: next(
                        states, libskillpack._ProcessLeaderState.EXITED_ANCHORED
                    ),
                ):
                    result, _ = libskillpack.run_stata_do(
                        Path("/fake/stata"),
                        do_file,
                        cwd,
                        completion_marker=marker,
                        timeout_seconds=1,
                    )

                self.assertEqual(natural_returncode, result.returncode)
                self.killpg.assert_called_once_with(
                    process.pid,
                    signal.SIGKILL,
                )
                self.killpg.reset_mock()


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


@unittest.skipUnless(
    hasattr(os, "fork")
    and (hasattr(os, "waitid") or sys.platform == "darwin"),
    "requires POSIX fork and non-reaping waitid support",
)
class ProcessGroupCleanupIntegrationTests(unittest.TestCase):


    def test_exited_leader_anchor_kills_same_group_descendant(self) -> None:
        with TemporaryDirectory(prefix="stata-group-anchor-") as temp_root:
            child_pid_path = Path(temp_root) / "child.pid"
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            source = (
                "import os, signal, time\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"    pid_path = {str(child_pid_path)!r}\n"
                "    with open(pid_path + '.tmp', 'w') as stream:\n"
                "        stream.write(str(os.getpid()))\n"
                "        stream.flush()\n"
                "        os.fsync(stream.fileno())\n"
                "    os.replace(pid_path + '.tmp', pid_path)\n"
                "    os.close(1)\n"
                "    os.close(2)\n"
                "    while True:\n"
                "        time.sleep(1)\n"
                "os._exit(0)\n"
            )
            with allow_detached_process():
                process = subprocess.Popen(
                    [sys.executable, "-c", source],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    pass_fds=(write_fd,),
                )
            os.close(write_fd)
            child_pid: int | None = None
            reached_eof = False
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if child_pid_path.exists():
                        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
                    if (
                        child_pid is not None
                        and libskillpack._process_leader_state(process)
                        is libskillpack._ProcessLeaderState.EXITED_ANCHORED
                    ):
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(child_pid)
                self.assertIs(
                    libskillpack._ProcessLeaderState.EXITED_ANCHORED,
                    libskillpack._process_leader_state(process),
                )
                self.assertIsNone(process.returncode)
                with self.assertRaises(BlockingIOError):
                    os.read(read_fd, 1)

                stopped = libskillpack._stop_process_group(process)

                self.assertTrue(stopped.cleanup_confirmed, stopped.diagnostic)
                self.assertFalse(stopped.leader_kill_sent)
                self.assertEqual(0, process.returncode)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        reached_eof = os.read(read_fd, 1) == b""
                    except BlockingIOError:
                        reached_eof = False
                    if reached_eof:
                        break
                    time.sleep(0.02)
                self.assertTrue(reached_eof)
            finally:
                if process.returncode is None:
                    libskillpack._force_cleanup_process(process)
                try:
                    os.close(read_fd)
                except OSError:
                    pass
                if child_pid is not None and not reached_eof:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_permission_error_with_same_group_descendant_fails_closed(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="stata-group-eperm-") as temp_root:
            child_pid_path = Path(temp_root) / "child.pid"
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            source = (
                "import os, signal, time\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                f"    pid_path = {str(child_pid_path)!r}\n"
                "    with open(pid_path + '.tmp', 'w') as stream:\n"
                "        stream.write(str(os.getpid()))\n"
                "        stream.flush()\n"
                "        os.fsync(stream.fileno())\n"
                "    os.replace(pid_path + '.tmp', pid_path)\n"
                "    os.close(1)\n"
                "    os.close(2)\n"
                "    while True:\n"
                "        time.sleep(1)\n"
                "os._exit(0)\n"
            )
            with allow_detached_process():
                process = subprocess.Popen(
                    [sys.executable, "-c", source],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    pass_fds=(write_fd,),
                )
            os.close(write_fd)
            child_pid: int | None = None
            reached_eof = False
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if child_pid_path.exists():
                        child_pid = int(
                            child_pid_path.read_text(encoding="utf-8")
                        )
                    if (
                        child_pid is not None
                        and libskillpack._process_leader_state(process)
                        is libskillpack._ProcessLeaderState.EXITED_ANCHORED
                    ):
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(child_pid)
                self.assertIs(
                    libskillpack._ProcessLeaderState.EXITED_ANCHORED,
                    libskillpack._process_leader_state(process),
                )
                self.assertEqual(process.pid, os.getpgid(child_pid))

                with patch.object(
                    libskillpack.os,
                    "killpg",
                    side_effect=PermissionError,
                ):
                    stopped = libskillpack._stop_process_group(process)

                self.assertFalse(stopped.cleanup_confirmed)
                self.assertIn(
                    "Could not confirm process-group termination",
                    stopped.diagnostic,
                )
                os.kill(child_pid, 0)
            finally:
                if process.returncode is None:
                    libskillpack._force_cleanup_process(process)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        try:
                            reached_eof = os.read(read_fd, 1) == b""
                        except BlockingIOError:
                            reached_eof = False
                        if reached_eof:
                            break
                        time.sleep(0.02)
                    self.assertTrue(
                        reached_eof,
                        "same-group descendant did not exit after test cleanup",
                    )
                try:
                    os.close(read_fd)
                except OSError:
                    pass


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


    def test_process_creation_is_denied_before_marker_success(self) -> None:
        with TemporaryDirectory(prefix="stata-contained-fork-") as temp_root:
            marker = "VALIDATION COMPLETE: fork-denied"
            cwd, do_file = self._make_run(temp_root, marker)
            stub = Path(temp_root) / "stata-stub"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess\n"
                "import sys\n"
                "try:\n"
                "    child = subprocess.Popen(\n"
                "        [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
                "        start_new_session=True,\n"
                "    )\n"
                "except OSError:\n"
                "    open('fork-denied', 'w').write('denied\\n')\n"
                "else:\n"
                "    open('escaped.pid', 'w').write(str(child.pid))\n"
                f"open('smoke.log', 'w').write('{marker}\\n')\n",
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
                "try:\n"
                "    child_pid = os.posix_spawn('/usr/bin/sleep', ['sleep', '30'], {})\n"
                "except OSError:\n"
                "    open('posix-spawn-denied', 'w').write('denied\\n')\n"
                "else:\n"
                "    open('escaped.pid', 'w').write(str(child_pid))\n"
                f"open('smoke.log', 'w').write('{marker}\\n')\n",
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
                f"open('smoke.log', 'w').write('{marker}\\n')\n",
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


    def test_marker_then_hang_fails_and_reaps_contained_leader(self) -> None:
        with TemporaryDirectory(prefix="stata-contained-marker-hang-") as temp_root:
            marker = "VALIDATION COMPLETE: marker-then-hang"
            cwd, do_file = self._make_run(temp_root, marker)
            stub = Path(temp_root) / "stata-stub"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import time\n"
                "open('leader.pid', 'w').write(str(os.getpid()))\n"
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
                timeout_seconds=1,
            )
            self.assertEqual(124, result.returncode)
            self.assertIn("did not exit naturally", result.stderr)
            leader_pid = int((cwd / "leader.pid").read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(leader_pid, 0)


class TimeoutAndChecksumTests(unittest.TestCase):


    @unittest.skipUnless(
        os.name == "posix",
        "POSIX process-group cleanup",
    )
    def test_run_command_timeout_terminates_descendants(self) -> None:
        with TemporaryDirectory(prefix="command-timeout-") as temp_root:
            pid_path = Path(temp_root) / "pids.txt"
            source = (
                "from pathlib import Path\n"
                "import os, subprocess, sys, time\n"
                "child = subprocess.Popen("
                "[sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"Path({str(pid_path)!r}).write_text("
                "f'{os.getpid()} {child.pid}\\n', encoding='utf-8')\n"
                "time.sleep(60)\n"
            )

            result = libskillpack.run_command(
                [sys.executable, "-c", source],
                timeout_seconds=0.5,
            )

            self.assertEqual(124, result.returncode)
            self.assertIn("timed out after 0.5 seconds", result.stderr)
            parent_pid, child_pid = (
                int(value)
                for value in pid_path.read_text(encoding="utf-8").split()
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
                        f"timed-out command process {process_id} still exists"
                    )

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


    def test_download_binary_rejects_oversized_stream_and_removes_temp(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="download-test-") as temp_root:
            root = Path(temp_root)
            destination = root / "sdk.c"
            with patch.object(
                libskillpack.urllib.request,
                "urlopen",
                return_value=FakeResponse(b"12345"),
            ):
                with self.assertRaisesRegex(ValueError, "4 byte limit"):
                    libskillpack.download_binary(
                        "https://example.invalid/sdk.c",
                        destination,
                        max_bytes=4,
                    )

            self.assertFalse(destination.exists())
            self.assertEqual([], list(root.glob(".sdk.c.*.tmp")))


    @unittest.skipUnless(
        hasattr(socket, "socketpair"),
        "requires a local socket pair",
    )
    def test_download_binary_deadline_bounds_real_trickling_socket(
        self,
    ) -> None:
        client, server = socket.socketpair()
        body_start = threading.Event()

        def serve_trickle() -> None:
            try:
                server.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 100\r\n"
                    b"Connection: close\r\n\r\n"
                )
                body_start.wait(timeout=1)
                for _ in range(100):
                    time.sleep(0.01)
                    server.sendall(b"x")
            except OSError:
                pass
            finally:
                server.close()

        writer = threading.Thread(target=serve_trickle)
        writer.start()
        response = http.client.HTTPResponse(client)
        response.begin()

        class StartedResponse:
            def __enter__(self) -> http.client.HTTPResponse:
                body_start.set()
                return response

            def __exit__(self, *args: object) -> None:
                del args
                response.close()

        try:
            with TemporaryDirectory(prefix="download-test-") as temp_root:
                root = Path(temp_root)
                destination = root / "sdk.c"
                started = time.monotonic()
                with patch.object(
                    libskillpack.urllib.request,
                    "urlopen",
                    return_value=StartedResponse(),
                ):
                    with self.assertRaisesRegex(
                        TimeoutError,
                        "timed out after 0.05 seconds",
                    ):
                        libskillpack.download_binary(
                            "https://example.invalid/trickle",
                            destination,
                            timeout_seconds=0.05,
                            max_bytes=256,
                        )

                self.assertLess(time.monotonic() - started, 0.25)
                self.assertFalse(destination.exists())
                self.assertEqual([], list(root.glob(".sdk.c.*.tmp")))
        finally:
            body_start.set()
            response.close()
            client.close()
            writer.join(timeout=2)
        self.assertFalse(writer.is_alive())

    def test_download_binary_partial_failure_preserves_destination(
        self,
    ) -> None:
        class PartialResponse(FakeResponse):
            def __init__(self) -> None:
                super().__init__(b"")
                self.read_count = 0

            def read(self, size: int = -1) -> bytes:
                del size
                self.read_count += 1
                if self.read_count == 1:
                    return b"partial"
                raise OSError("connection reset")

        with TemporaryDirectory(prefix="download-test-") as temp_root:
            root = Path(temp_root)
            destination = root / "sdk.c"
            destination.write_bytes(b"reviewed")
            with patch.object(
                libskillpack.urllib.request,
                "urlopen",
                return_value=PartialResponse(),
            ):
                with self.assertRaisesRegex(OSError, "connection reset"):
                    libskillpack.download_binary(
                        "https://example.invalid/sdk.c",
                        destination,
                    )

            self.assertEqual(b"reviewed", destination.read_bytes())
            self.assertEqual([], list(root.glob(".sdk.c.*.tmp")))


if __name__ == "__main__":
    unittest.main()
