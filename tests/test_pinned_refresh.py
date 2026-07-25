from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import os
import shlex
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import fetch_upstream  # noqa: E402


CORE_PATH = Path("plugins/stata/skills/stata/references/basics.md")
PACKAGE_PATH = Path("plugins/stata/skills/stata/packages/estout.md")
ADDED_PACKAGE_PATH = Path("plugins/stata/skills/stata/packages/coefplot.md")
NESTED_CORE_PATH = Path(
    "plugins/stata/skills/stata/references/advanced/nested.md"
)
PLUGIN_PATH = Path(
    "plugins/stata-c-plugins/skills/stata-c-plugins/references/cpp_plugins.md"
)
IGNORED_COLLISION_PATH = Path("local-cache.txt")


def run_git(repository: Path, *arguments: str) -> CompletedProcess[str]:
    arguments_list = list(arguments)
    if (
        arguments_list
        and arguments_list[0] == "clone"
        and "--no-hardlinks" not in arguments_list
        and "--no-local" not in arguments_list
    ):
        arguments_list.insert(1, "--no-hardlinks")
    return subprocess.run(
        [
            "git",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            "-C",
            str(repository),
            *arguments_list,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class UpstreamFixture:
    def __init__(self, root: Path) -> None:
        self.repository = root / "remote"
        self.repository.mkdir()
        run_git(self.repository, "init")
        run_git(self.repository, "config", "user.name", "Refresh Test")
        run_git(self.repository, "config", "user.email", "refresh@example.invalid")

        self.first_files = {
            CORE_PATH: "# Basics\nfirst\n",
            NESTED_CORE_PATH: "# Nested\nfirst\n",
            PACKAGE_PATH: "# estout\nfirst\n",
            PLUGIN_PATH: "# C++ plugins\nfirst\n",
        }
        self._write_files(self.first_files)
        (self.repository / ".gitignore").write_text(
            f"{IGNORED_COLLISION_PATH}\n",
            encoding="utf-8",
        )
        run_git(self.repository, "add", ".")
        run_git(self.repository, "commit", "-m", "first")
        self.first_commit = run_git(
            self.repository, "rev-parse", "HEAD"
        ).stdout.strip()

        self.second_files = {
            **self.first_files,
            CORE_PATH: "# Basics\nsecond\n",
            ADDED_PACKAGE_PATH: "# coefplot\nsecond\n",
        }
        self._write_files(self.second_files)
        (self.repository / IGNORED_COLLISION_PATH).write_text(
            "upstream cache\n",
            encoding="utf-8",
        )
        run_git(self.repository, "add", ".")
        run_git(self.repository, "add", "-f", str(IGNORED_COLLISION_PATH))
        run_git(self.repository, "commit", "-m", "second")
        self.second_commit = run_git(
            self.repository, "rev-parse", "HEAD"
        ).stdout.strip()

    def _write_files(self, files: dict[Path, str]) -> None:
        for relative_path, text in files.items():
            path = self.repository / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def write_first_lock(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "repository": {
                "url": str(self.repository),
                "commit": self.first_commit,
                "expected_commit": self.first_commit,
            },
            "files": {
                str(relative_path): {"sha256": digest(text)}
                for relative_path, text in sorted(
                    self.first_files.items(), key=lambda item: str(item[0])
                )
            },
        }
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False),
            encoding="utf-8",
        )


class PinnedRefreshTests(unittest.TestCase):
    def test_exact_commit_is_required(self) -> None:
        for value in (
            "main",
            "HEAD",
            "a" * 39,
            "a" * 41,
            "g" * 40,
            "a" * 39 + "~",
        ):
            with self.subTest(value=value), self.assertRaises(
                fetch_upstream.argparse.ArgumentTypeError
            ):
                fetch_upstream.exact_commit(value)

        self.assertEqual("a" * 40, fetch_upstream.exact_commit("A" * 40))

    def test_git_environment_ignores_global_and_system_configuration(self) -> None:
        injected = {
            "GIT_ASKPASS": "/tmp/foreign-askpass",
            "GIT_CONFIG_COUNT": "1",
            "GIT_EXEC_PATH": "/tmp/foreign-git-exec-path",
            "GIT_CONFIG_GLOBAL": "/tmp/foreign-global-config",
            "GIT_CONFIG_KEY_0": "include.path",
            "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/foreign-hooks'",
            "GIT_CONFIG_SYSTEM": "/tmp/foreign-system-config",
            "GIT_CONFIG_VALUE_0": "/tmp/foreign-include",
            "GIT_PROXY_COMMAND": "/tmp/foreign-proxy",
            "GIT_SSH": "/tmp/foreign-ssh",
            "GIT_SSH_COMMAND": "/tmp/foreign-ssh-command",
            "GIT_TEMPLATE_DIR": "/tmp/foreign-template",
            "SSH_ASKPASS": "/tmp/foreign-ssh-askpass",
            "SSH_ASKPASS_REQUIRE": "force",
        }
        with patch.dict(fetch_upstream.os.environ, injected, clear=False):
            environment = fetch_upstream.sanitized_git_environment()

        self.assertEqual(fetch_upstream.os.devnull, environment["GIT_CONFIG_GLOBAL"])
        self.assertEqual("1", environment["GIT_CONFIG_NOSYSTEM"])
        self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])
        for variable in (
            "GIT_ASKPASS",
            "GIT_CONFIG_COUNT",
            "GIT_EXEC_PATH",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_VALUE_0",
            "GIT_PROXY_COMMAND",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_TEMPLATE_DIR",
            "SSH_ASKPASS",
            "SSH_ASKPASS_REQUIRE",
        ):
            self.assertNotIn(variable, environment)

    def test_timeout_terminates_the_entire_git_process_group(self) -> None:
        with TemporaryDirectory(prefix="pinned-process-group-") as temp_root:
            root = Path(temp_root)
            checkout_fd = os.open(root, fetch_upstream.DIRECTORY_OPEN_FLAGS)
            process = Mock(pid=12345, returncode=-9)
            process.communicate.side_effect = [
                subprocess.TimeoutExpired(["git"], 1),
                ("partial stdout", "partial stderr"),
            ]
            try:
                with patch.object(
                    fetch_upstream.subprocess,
                    "Popen",
                    return_value=process,
                ) as mocked_popen, patch.object(
                    fetch_upstream,
                    "terminate_process_group",
                ) as terminate:
                    result = fetch_upstream.run_anchored_git(
                        ["git", "status"],
                        checkout_fd,
                        timeout_seconds=1,
                    )
            finally:
                os.close(checkout_fd)

        self.assertEqual(124, result.returncode)
        self.assertIn("timed out", result.stderr.lower())
        terminate.assert_called_once_with(process)
        self.assertTrue(mocked_popen.call_args.kwargs["start_new_session"])
        command = mocked_popen.call_args.args[0]
        self.assertIn("maintenance.auto=false", command)
        self.assertIn("gc.auto=0", command)

    def test_timeout_does_not_block_on_stuck_text_pipes_after_kill(self) -> None:
        with TemporaryDirectory(prefix="pinned-stuck-text-pipes-") as temp_root:
            root = Path(temp_root)
            checkout_fd = os.open(root, fetch_upstream.DIRECTORY_OPEN_FLAGS)
            process = Mock(pid=12345, returncode=-9)
            process.stdout = Mock()
            process.stderr = Mock()
            process.communicate.side_effect = [
                subprocess.TimeoutExpired(
                    ["git"],
                    1,
                    output="partial stdout",
                    stderr="partial stderr",
                ),
                subprocess.TimeoutExpired(
                    ["git"],
                    fetch_upstream.POST_KILL_REAP_TIMEOUT_SECONDS,
                    output="post-kill stdout",
                    stderr="post-kill stderr",
                ),
            ]
            process.wait.return_value = -9
            try:
                with patch.object(
                    fetch_upstream.subprocess,
                    "Popen",
                    return_value=process,
                ), patch.object(
                    fetch_upstream,
                    "terminate_process_group",
                ) as terminate:
                    result = fetch_upstream.run_anchored_git(
                        ["git", "status"],
                        checkout_fd,
                        timeout_seconds=1,
                    )
            finally:
                os.close(checkout_fd)

        self.assertEqual(124, result.returncode)
        self.assertIn("partial stdout", result.stdout)
        self.assertIn("post-kill pipe closure timed out", result.stderr.lower())
        self.assertEqual(2, process.communicate.call_count)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()
        process.wait.assert_called_once_with(
            timeout=fetch_upstream.POST_KILL_REAP_TIMEOUT_SECONDS
        )
        self.assertGreaterEqual(terminate.call_count, 2)

    def test_timeout_does_not_block_on_stuck_binary_pipes_after_kill(self) -> None:
        with TemporaryDirectory(prefix="pinned-stuck-binary-pipes-") as temp_root:
            root = Path(temp_root)
            checkout_fd = os.open(root, fetch_upstream.DIRECTORY_OPEN_FLAGS)
            process = Mock(pid=12345, returncode=-9)
            process.stdout = Mock()
            process.stderr = Mock()
            process.communicate.side_effect = [
                subprocess.TimeoutExpired(
                    ["git"],
                    1,
                    output=b"partial stdout",
                    stderr=b"partial stderr",
                ),
                subprocess.TimeoutExpired(
                    ["git"],
                    fetch_upstream.POST_KILL_REAP_TIMEOUT_SECONDS,
                    output=b"post-kill stdout",
                    stderr=b"post-kill stderr",
                ),
            ]
            process.wait.return_value = -9
            try:
                with patch.object(
                    fetch_upstream.subprocess,
                    "Popen",
                    return_value=process,
                ) as mocked_popen, patch.object(
                    fetch_upstream,
                    "terminate_process_group",
                ) as terminate:
                    result = fetch_upstream.run_anchored_git_bytes(
                        ["git", "status"],
                        checkout_fd,
                        timeout_seconds=1,
                    )
            finally:
                os.close(checkout_fd)

        self.assertEqual(124, result.returncode)
        self.assertIn(b"partial stdout", result.stdout)
        self.assertIn(
            b"post-kill pipe closure timed out",
            result.stderr.lower(),
        )
        self.assertEqual(2, process.communicate.call_count)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()
        process.wait.assert_called_once_with(
            timeout=fetch_upstream.POST_KILL_REAP_TIMEOUT_SECONDS
        )
        self.assertGreaterEqual(terminate.call_count, 2)
        command = mocked_popen.call_args.args[0]
        self.assertIn("maintenance.auto=false", command)
        self.assertIn("gc.auto=0", command)

    def test_interrupt_terminates_and_reaps_text_git_process_group(self) -> None:
        with TemporaryDirectory(prefix="pinned-process-interrupt-") as temp_root:
            root = Path(temp_root)
            checkout_fd = os.open(root, fetch_upstream.DIRECTORY_OPEN_FLAGS)
            process = Mock(pid=12345, returncode=-9)
            process.communicate.side_effect = [
                KeyboardInterrupt(),
                ("", ""),
            ]
            try:
                with patch.object(
                    fetch_upstream.subprocess,
                    "Popen",
                    return_value=process,
                ), patch.object(
                    fetch_upstream,
                    "terminate_process_group",
                ) as terminate, self.assertRaises(KeyboardInterrupt):
                    fetch_upstream.run_anchored_git(
                        ["git", "status"],
                        checkout_fd,
                    )
            finally:
                os.close(checkout_fd)

        terminate.assert_called_once_with(process)
        self.assertEqual(2, process.communicate.call_count)

    def test_interrupt_terminates_and_reaps_binary_git_process_group(self) -> None:
        with TemporaryDirectory(prefix="pinned-process-interrupt-") as temp_root:
            root = Path(temp_root)
            checkout_fd = os.open(root, fetch_upstream.DIRECTORY_OPEN_FLAGS)
            process = Mock(pid=12345, returncode=-9)
            process.communicate.side_effect = [
                KeyboardInterrupt(),
                (b"", b""),
            ]
            try:
                with patch.object(
                    fetch_upstream.subprocess,
                    "Popen",
                    return_value=process,
                ), patch.object(
                    fetch_upstream,
                    "terminate_process_group",
                ) as terminate, self.assertRaises(KeyboardInterrupt):
                    fetch_upstream.run_anchored_git_bytes(
                        ["git", "status"],
                        checkout_fd,
                    )
            finally:
                os.close(checkout_fd)

        terminate.assert_called_once_with(process)
        self.assertEqual(2, process.communicate.call_count)

    def test_symlinked_raw_root_cannot_replace_an_external_report(self) -> None:
        with TemporaryDirectory(prefix="pinned-report-symlink-") as temp_root:
            root = Path(temp_root)
            repository_root = root / "repository"
            external_root = root / "external"
            repository_root.mkdir()
            report_path = (
                repository_root
                / "raw"
                / "candidates"
                / "upstream-comparison.yaml"
            )
            external_report = (
                external_root / "candidates" / "upstream-comparison.yaml"
            )
            external_report.parent.mkdir(parents=True)
            external_report.write_text("external: preserve\n", encoding="utf-8")
            (repository_root / "raw").symlink_to(
                external_root,
                target_is_directory=True,
            )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=repository_root,
                RAW_ROOT=repository_root / "raw",
                UPSTREAM_REPO_DIR=(
                    repository_root / "raw" / "upstream" / "stata-skill"
                ),
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                "external: preserve\n",
                external_report.read_text(encoding="utf-8"),
            )

    def test_symlinked_checkout_cannot_modify_an_external_repository(self) -> None:
        with TemporaryDirectory(prefix="pinned-checkout-symlink-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            repository_root = root / "repository"
            raw_root = repository_root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            external_checkout = root / "external-checkout"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = repository_root / "locks" / "upstream.yaml"
            repository_root.mkdir()
            run_git(root, "clone", str(fixture.repository), str(external_checkout))
            dirty_path = external_checkout / CORE_PATH
            dirty_path.write_text("external dirty work\n", encoding="utf-8")
            original_head = run_git(
                external_checkout,
                "rev-parse",
                "HEAD",
            ).stdout.strip()
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            checkout.symlink_to(external_checkout, target_is_directory=True)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=repository_root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                original_head,
                run_git(external_checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                "external dirty work\n",
                dirty_path.read_text(encoding="utf-8"),
            )

    def test_symlinked_checkout_ancestor_is_refused(self) -> None:
        with TemporaryDirectory(prefix="pinned-checkout-ancestor-") as temp_root:
            root = Path(temp_root)
            repository_root = root / "repository"
            external_upstream = root / "external-upstream"
            repository_root.mkdir()
            external_upstream.mkdir()
            (external_upstream / "sentinel.txt").write_text(
                "preserve\n",
                encoding="utf-8",
            )
            raw_root = repository_root / "raw"
            raw_root.mkdir()
            (raw_root / "upstream").symlink_to(
                external_upstream,
                target_is_directory=True,
            )
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=repository_root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=raw_root / "upstream" / "stata-skill",
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                "preserve\n",
                (external_upstream / "sentinel.txt").read_text(encoding="utf-8"),
            )
            self.assertFalse((external_upstream / "stata-skill").exists())

    def test_clean_exact_origin_checkout_is_adopted_once(self) -> None:
        with TemporaryDirectory(prefix="pinned-unowned-checkout-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            repository_root = root / "repository"
            raw_root = repository_root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = repository_root / "locks" / "upstream.yaml"
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            original_head = run_git(checkout, "rev-parse", "HEAD").stdout.strip()
            marker = checkout / ".git" / fetch_upstream.CHECKOUT_OWNER_MARKER
            self.assertFalse(marker.exists())
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=repository_root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertNotEqual(fixture.first_commit, original_head)
            self.assertEqual(
                fixture.first_commit,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                fetch_upstream.CHECKOUT_OWNER_CONTENT,
                marker.read_text(encoding="utf-8"),
            )
            self.assertTrue(report_path.is_file())

    def test_dirty_unmarked_checkout_is_not_adopted(self) -> None:
        with TemporaryDirectory(prefix="pinned-dirty-adoption-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            repository_root = root / "repository"
            raw_root = repository_root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = repository_root / "locks" / "upstream.yaml"
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            original_head = run_git(checkout, "rev-parse", "HEAD").stdout.strip()
            dirty_path = checkout / CORE_PATH
            dirty_path.write_text("unmarked local work\n", encoding="utf-8")
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=repository_root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                original_head,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                "unmarked local work\n",
                dirty_path.read_text(encoding="utf-8"),
            )
            self.assertFalse(
                (checkout / ".git" / fetch_upstream.CHECKOUT_OWNER_MARKER).exists()
            )
            self.assertFalse(report_path.exists())

    def test_oversized_sparse_owner_marker_is_rejected_before_git_launch(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-owner-marker-size-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            marker = checkout / ".git" / fetch_upstream.CHECKOUT_OWNER_MARKER
            with marker.open("wb") as handle:
                handle.truncate(1 << 40)
            marker_size = marker.stat().st_size

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertEqual(marker_size, marker.stat().st_size)
            self.assertFalse(report_path.exists())

    def test_wrong_origin_unmarked_checkout_is_not_adopted(self) -> None:
        with TemporaryDirectory(prefix="pinned-origin-adoption-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            repository_root = root / "repository"
            raw_root = repository_root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = repository_root / "locks" / "upstream.yaml"
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            original_head = run_git(checkout, "rev-parse", "HEAD").stdout.strip()
            fixture.write_first_lock(lock_path)
            lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
            lock["repository"]["url"] = "https://example.invalid/wrong-origin.git"
            lock_path.write_text(
                yaml.safe_dump(lock, sort_keys=False),
                encoding="utf-8",
            )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=repository_root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL="https://example.invalid/wrong-origin.git",
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                original_head,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertFalse(
                (checkout / ".git" / fetch_upstream.CHECKOUT_OWNER_MARKER).exists()
            )
            self.assertFalse(report_path.exists())

    def test_lock_repository_mismatch_is_rejected_before_git_or_quarantine(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-lock-repository-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            lock = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
            lock["repository"]["url"] = "https://example.invalid/other.git"
            lock_path.write_text(
                yaml.safe_dump(lock, sort_keys=False),
                encoding="utf-8",
            )
            report_path.parent.mkdir(parents=True)
            report_bytes = (
                "schema_version: 1\n"
                "report_type: upstream-comparison\n"
                f"report_owner: {fetch_upstream.REPORT_OWNER}\n"
            ).encode("utf-8")
            report_path.write_bytes(report_bytes)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen, patch("builtins.print") as mocked_print:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            output = "\n".join(
                str(call.args[0]) for call in mocked_print.call_args_list
            )
            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertIn("repository.url", output)
            self.assertIn("configured upstream repository", output)
            self.assertEqual(report_bytes, report_path.read_bytes())
            self.assertFalse(checkout.exists())

    def test_malformed_matching_lock_is_rejected_before_git_or_quarantine(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-malformed-lock-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            lock_path.parent.mkdir()
            lock_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "repository": {
                            "url": str(fixture.repository),
                            "commit": fixture.first_commit,
                            "expected_commit": fixture.first_commit,
                        },
                        "files": [],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            report_path.parent.mkdir(parents=True)
            report_bytes = (
                "schema_version: 1\n"
                "report_type: upstream-comparison\n"
                f"report_owner: {fetch_upstream.REPORT_OWNER}\n"
            ).encode("utf-8")
            report_path.write_bytes(report_bytes)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen, patch("builtins.print") as mocked_print:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            output = "\n".join(
                str(call.args[0]) for call in mocked_print.call_args_list
            )
            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertIn("files must be a mapping", output)
            self.assertEqual(report_bytes, report_path.read_bytes())
            self.assertEqual([], list(report_path.parent.glob("*.stale")))
            self.assertFalse(checkout.exists())

    def test_comparison_report_rejects_inventory_repository_mismatch(self) -> None:
        with TemporaryDirectory(prefix="pinned-inventory-repository-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            inventory = {
                "repository": "https://example.invalid/other.git",
                "commit": fixture.first_commit,
                "inventory": {
                    "core": [],
                    "packages": [],
                    "plugins": [],
                },
            }

            with patch.multiple(
                fetch_upstream,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), self.assertRaisesRegex(
                RuntimeError,
                "Candidate inventory repository does not exactly match",
            ):
                fetch_upstream.build_comparison_report(
                    inventory,
                    fixture.first_commit,
                )

    def test_linked_worktree_checkout_is_rejected_without_mutation(self) -> None:
        with TemporaryDirectory(prefix="pinned-linked-worktree-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            primary = root / "primary"
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            run_git(root, "clone", str(fixture.repository), str(primary))
            checkout.parent.mkdir(parents=True)
            run_git(
                primary,
                "worktree",
                "add",
                "--detach",
                str(checkout),
                fixture.first_commit,
            )
            gitfile = checkout / ".git"
            gitfile_before = gitfile.read_bytes()
            head_before = run_git(checkout, "rev-parse", "HEAD").stdout.strip()

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(gitfile_before, gitfile.read_bytes())
            self.assertEqual(
                head_before,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertFalse(report_path.exists())

    def test_external_git_commondir_is_rejected_without_access(self) -> None:
        with TemporaryDirectory(prefix="pinned-commondir-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            external = root / "external-checkout"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            run_git(root, "clone", str(fixture.repository), str(external))
            external_config = (external / ".git" / "config").read_bytes()
            external_head = run_git(external, "rev-parse", "HEAD").stdout.strip()
            (checkout / ".git" / "commondir").write_text(
                str(external / ".git"),
                encoding="utf-8",
            )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                external_config,
                (external / ".git" / "config").read_bytes(),
            )
            self.assertEqual(
                external_head,
                run_git(external, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertFalse(report_path.exists())

    def test_external_git_object_alternates_are_rejected_without_access(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-alternates-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            external = root / "external-checkout"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            run_git(root, "clone", str(fixture.repository), str(external))
            external_config = (external / ".git" / "config").read_bytes()
            external_head = run_git(external, "rev-parse", "HEAD").stdout.strip()
            info_dir = checkout / ".git" / "objects" / "info"
            info_dir.mkdir(exist_ok=True)
            (info_dir / "alternates").write_text(
                f"{external / '.git' / 'objects'}\n",
                encoding="utf-8",
            )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                external_config,
                (external / ".git" / "config").read_bytes(),
            )
            self.assertEqual(
                external_head,
                run_git(external, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertFalse(report_path.exists())

    def test_external_git_objects_symlink_is_rejected_without_access(self) -> None:
        with TemporaryDirectory(prefix="pinned-objects-symlink-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            external = root / "external-checkout"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            run_git(root, "clone", str(fixture.repository), str(external))
            external_config = (external / ".git" / "config").read_bytes()
            external_head = run_git(external, "rev-parse", "HEAD").stdout.strip()
            objects_path = checkout / ".git" / "objects"
            objects_path.rename(checkout / ".git" / "objects-owned")
            objects_path.symlink_to(
                external / ".git" / "objects",
                target_is_directory=True,
            )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                external_config,
                (external / ".git" / "config").read_bytes(),
            )
            self.assertEqual(
                external_head,
                run_git(external, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertFalse(report_path.exists())

    def test_nested_git_write_namespace_symlinks_are_rejected_before_git_launch(
        self,
    ) -> None:
        namespace_paths = (
            Path("objects") / "pack",
            Path("refs") / "heads",
            Path("logs") / "refs",
        )
        for relative_namespace in namespace_paths:
            with self.subTest(namespace=relative_namespace), TemporaryDirectory(
                prefix="pinned-nested-git-symlink-"
            ) as temp_root:
                root = Path(temp_root)
                fixture = UpstreamFixture(root)
                raw_root = root / "raw"
                checkout = raw_root / "upstream" / "stata-skill"
                report_path = (
                    raw_root / "candidates" / "upstream-comparison.yaml"
                )
                lock_path = root / "locks" / "upstream.yaml"
                external = root / "external-git-write-target"
                external.mkdir()
                sentinel = external / "sentinel.txt"
                sentinel.write_text("preserve\n", encoding="utf-8")
                fixture.write_first_lock(lock_path)
                checkout.parent.mkdir(parents=True)
                run_git(root, "clone", str(fixture.repository), str(checkout))
                namespace = checkout / ".git" / relative_namespace
                namespace.parent.mkdir(parents=True, exist_ok=True)
                if namespace.exists():
                    namespace.rename(namespace.with_name(f"{namespace.name}-owned"))
                namespace.symlink_to(external, target_is_directory=True)
                external_before = {
                    path.name: path.read_bytes()
                    for path in external.iterdir()
                    if path.is_file()
                }

                with patch.multiple(
                    fetch_upstream,
                    REPO_ROOT=root,
                    RAW_ROOT=raw_root,
                    UPSTREAM_REPO_DIR=checkout,
                    UPSTREAM_REPO_URL=str(fixture.repository),
                    UPSTREAM_LOCK_PATH=lock_path,
                ), patch.object(
                    fetch_upstream.subprocess,
                    "Popen",
                ) as mocked_popen:
                    exit_code = fetch_upstream.main(
                        [
                            "--upstream-ref",
                            fixture.first_commit,
                            "--offline",
                            "--report",
                            str(report_path),
                        ]
                    )

                self.assertEqual(1, exit_code)
                mocked_popen.assert_not_called()
                self.assertEqual(
                    external_before,
                    {
                        path.name: path.read_bytes()
                        for path in external.iterdir()
                        if path.is_file()
                    },
                )
                self.assertEqual("preserve\n", sentinel.read_text(encoding="utf-8"))
                self.assertFalse(report_path.exists())

    def test_special_entry_in_git_write_namespace_is_rejected_before_git_launch(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-git-special-entry-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            fifo = checkout / ".git" / "logs" / "refs" / "unsafe-fifo"
            fifo.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(fifo)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertFalse(report_path.exists())

    def test_fetch_head_symlink_cannot_overwrite_external_bytes(self) -> None:
        with TemporaryDirectory(prefix="pinned-fetch-head-symlink-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            owner_marker = (
                checkout / ".git" / fetch_upstream.CHECKOUT_OWNER_MARKER
            )
            owner_marker.write_text(
                fetch_upstream.CHECKOUT_OWNER_CONTENT,
                encoding="utf-8",
            )
            external = root / "external-fetch-head"
            sentinel = b"external fetch sentinel\n"
            external.write_bytes(sentinel)
            fetch_head = checkout / ".git" / "FETCH_HEAD"
            if fetch_head.exists():
                fetch_head.rename(checkout / ".git" / "FETCH_HEAD-owned")
            fetch_head.symlink_to(external)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertEqual(sentinel, external.read_bytes())
            self.assertTrue(fetch_head.is_symlink())
            self.assertFalse(report_path.exists())

    def test_git_log_hardlink_cannot_append_to_external_bytes(self) -> None:
        with TemporaryDirectory(prefix="pinned-log-hardlink-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            owner_marker = (
                checkout / ".git" / fetch_upstream.CHECKOUT_OWNER_MARKER
            )
            owner_marker.write_text(
                fetch_upstream.CHECKOUT_OWNER_CONTENT,
                encoding="utf-8",
            )
            external = root / "external-log-head"
            sentinel = b"external log sentinel\n"
            external.write_bytes(sentinel)
            log_head = checkout / ".git" / "logs" / "HEAD"
            log_head.rename(log_head.with_name("HEAD-owned"))
            os.link(external, log_head)
            self.assertGreater(log_head.stat().st_nlink, 1)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertEqual(sentinel, external.read_bytes())
            self.assertFalse(report_path.exists())

    def test_git_object_namespace_hardlinks_are_rejected_before_git_launch(
        self,
    ) -> None:
        for relative_path in (
            Path("objects") / "info" / "packs",
            Path("objects") / "pack" / "multi-pack-index",
        ):
            with self.subTest(path=relative_path), TemporaryDirectory(
                prefix="pinned-object-hardlink-"
            ) as temp_root:
                root = Path(temp_root)
                fixture = UpstreamFixture(root)
                raw_root = root / "raw"
                checkout = raw_root / "upstream" / "stata-skill"
                report_path = (
                    raw_root / "candidates" / "upstream-comparison.yaml"
                )
                lock_path = root / "locks" / "upstream.yaml"
                fixture.write_first_lock(lock_path)
                checkout.parent.mkdir(parents=True)
                run_git(root, "clone", str(fixture.repository), str(checkout))
                external = root / "external-object-metadata"
                sentinel = b"external object metadata sentinel\n"
                external.write_bytes(sentinel)
                hardlink = checkout / ".git" / relative_path
                hardlink.parent.mkdir(parents=True, exist_ok=True)
                if hardlink.exists():
                    hardlink.rename(
                        hardlink.with_name(f"{hardlink.name}-owned")
                    )
                os.link(external, hardlink)
                self.assertGreater(hardlink.stat().st_nlink, 1)

                with patch.multiple(
                    fetch_upstream,
                    REPO_ROOT=root,
                    RAW_ROOT=raw_root,
                    UPSTREAM_REPO_DIR=checkout,
                    UPSTREAM_REPO_URL=str(fixture.repository),
                    UPSTREAM_LOCK_PATH=lock_path,
                ), patch.object(
                    fetch_upstream.subprocess,
                    "Popen",
                ) as mocked_popen:
                    exit_code = fetch_upstream.main(
                        [
                            "--upstream-ref",
                            fixture.first_commit,
                            "--offline",
                            "--report",
                            str(report_path),
                        ]
                    )

                self.assertEqual(1, exit_code)
                mocked_popen.assert_not_called()
                self.assertEqual(sentinel, external.read_bytes())
                self.assertFalse(report_path.exists())

    def test_git_directory_must_share_the_checkout_device(self) -> None:
        with TemporaryDirectory(prefix="pinned-git-device-") as temp_root:
            checkout = Path(temp_root) / "checkout"
            git_dir = checkout / ".git"
            git_dir.mkdir(parents=True)
            checkout_fd = os.open(checkout, fetch_upstream.DIRECTORY_OPEN_FLAGS)
            git_dir_fd = os.open(git_dir, fetch_upstream.DIRECTORY_OPEN_FLAGS)
            context = fetch_upstream.GitMetadataContext(
                checkout_fd=checkout_fd,
                git_dir_fd=git_dir_fd,
            )
            real_fstat = fetch_upstream.os.fstat

            def cross_device_git_dir(file_descriptor: int) -> os.stat_result:
                metadata = real_fstat(file_descriptor)
                if file_descriptor != git_dir_fd:
                    return metadata
                fields = list(metadata)
                fields[2] = metadata.st_dev + 1
                return os.stat_result(fields)

            try:
                with patch.object(
                    fetch_upstream.os,
                    "fstat",
                    side_effect=cross_device_git_dir,
                ), self.assertRaisesRegex(
                    RuntimeError,
                    "same device as the dedicated checkout",
                ):
                    fetch_upstream.assert_dedicated_git_layout(context)
            finally:
                context.close()

    def test_regular_git_pack_contents_remain_supported(self) -> None:
        with TemporaryDirectory(prefix="pinned-safe-pack-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            run_git(fixture.repository, "gc")
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(
                root,
                "clone",
                "--no-local",
                str(fixture.repository),
                str(checkout),
            )
            self.assertTrue(
                list((checkout / ".git" / "objects" / "pack").glob("*.pack"))
            )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertTrue(report_path.is_file())

    def test_extensions_worktree_config_is_rejected_before_git_launch(self) -> None:
        with TemporaryDirectory(prefix="pinned-worktree-config-ext-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            run_git(
                checkout,
                "config",
                "extensions.worktreeConfig",
                "true",
            )
            head_before = (checkout / ".git" / "HEAD").read_bytes()

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertEqual(head_before, (checkout / ".git" / "HEAD").read_bytes())
            self.assertFalse(report_path.exists())

    def test_config_worktree_file_is_rejected_before_git_launch(self) -> None:
        with TemporaryDirectory(prefix="pinned-worktree-config-file-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            config_worktree = checkout / ".git" / "config.worktree"
            config_worktree.write_text("[core]\n\tbare = false\n", encoding="utf-8")
            head_before = (checkout / ".git" / "HEAD").read_bytes()

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertEqual(head_before, (checkout / ".git" / "HEAD").read_bytes())
            self.assertEqual(
                "[core]\n\tbare = false\n",
                config_worktree.read_text(encoding="utf-8"),
            )
            self.assertFalse(report_path.exists())

    def test_worktree_fsmonitor_bypass_is_rejected_without_execution(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-worktree-fsmonitor-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            marker = root / "fsmonitor-executed"
            hook = root / "malicious-fsmonitor"
            hook.write_text(
                "#!/bin/sh\n"
                f"/usr/bin/touch {shlex.quote(str(marker))}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            run_git(
                checkout,
                "config",
                "extensions.worktreeConfig",
                "true",
            )
            run_git(
                checkout,
                "config",
                "--worktree",
                "core.fsmonitor",
                str(hook),
            )
            config_worktree = checkout / ".git" / "config.worktree"
            config_before = config_worktree.read_bytes()
            head_before = (checkout / ".git" / "HEAD").read_bytes()

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertFalse(marker.exists())
            self.assertEqual(head_before, (checkout / ".git" / "HEAD").read_bytes())
            self.assertEqual(config_before, config_worktree.read_bytes())
            self.assertFalse(report_path.exists())

    def test_core_fsmonitor_cannot_execute_before_rejection(self) -> None:
        with TemporaryDirectory(prefix="pinned-fsmonitor-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            marker = root / "fsmonitor-executed"
            hook = root / "malicious-fsmonitor"
            hook.write_text(
                "#!/bin/sh\n"
                f"/usr/bin/touch {marker}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
            fixture.write_first_lock(lock_path)
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            run_git(checkout, "config", "core.fsmonitor", str(hook))

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertFalse(marker.exists())
            self.assertFalse(report_path.exists())

    def test_dirty_owned_checkout_is_not_replaced(self) -> None:
        with TemporaryDirectory(prefix="pinned-dirty-checkout-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                first_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )
                dirty_path = checkout / CORE_PATH
                dirty_path.write_text("local work\n", encoding="utf-8")
                second_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.second_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, first_exit)
            self.assertEqual(1, second_exit)
            self.assertEqual(
                fixture.first_commit,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                "local work\n",
                dirty_path.read_text(encoding="utf-8"),
            )
            self.assertFalse(report_path.exists())

    def test_untracked_owned_checkout_file_is_not_replaced(self) -> None:
        with TemporaryDirectory(prefix="pinned-untracked-checkout-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                first_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )
                untracked = checkout / "local-notes.txt"
                untracked.write_text("local work\n", encoding="utf-8")
                second_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.second_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, first_exit)
            self.assertEqual(1, second_exit)
            self.assertEqual(
                fixture.first_commit,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                "local work\n",
                untracked.read_text(encoding="utf-8"),
            )
            self.assertFalse(report_path.exists())

    def test_ignored_owned_checkout_file_is_not_replaced(self) -> None:
        with TemporaryDirectory(prefix="pinned-ignored-checkout-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                first_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )
                ignored = checkout / IGNORED_COLLISION_PATH
                ignored.write_text("ignored local work\n", encoding="utf-8")
                second_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.second_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, first_exit)
            self.assertEqual(1, second_exit)
            self.assertEqual(
                fixture.first_commit,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                "ignored local work\n",
                ignored.read_text(encoding="utf-8"),
            )
            self.assertFalse(report_path.exists())

    def test_late_ignored_file_injection_is_preserved_and_aborts_checkout(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-late-ignored-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                first_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            ignored = checkout / IGNORED_COLLISION_PATH
            real_run_anchored_git = fetch_upstream.run_anchored_git
            injected = False
            protected_checkout_seen = False

            def inject_after_final_clean_check(
                arguments: list[str],
                context: int | fetch_upstream.GitMetadataContext,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                nonlocal injected, protected_checkout_seen
                if (
                    not injected
                    and arguments[:3] == ["git", "checkout", "--detach"]
                ):
                    protected_checkout_seen = "--no-overwrite-ignore" in arguments
                    ignored.write_bytes(b"late foreign ignored bytes\n")
                    injected = True
                return real_run_anchored_git(
                    arguments,
                    context,
                    timeout_seconds=timeout_seconds,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_anchored_git",
                side_effect=inject_after_final_clean_check,
            ):
                second_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.second_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, first_exit)
            self.assertEqual(1, second_exit)
            self.assertTrue(injected)
            self.assertTrue(protected_checkout_seen)
            self.assertEqual(b"late foreign ignored bytes\n", ignored.read_bytes())
            self.assertEqual(
                fixture.first_commit,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertFalse(report_path.exists())

    def test_checkout_path_substitution_cannot_modify_external_repository(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-checkout-race-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            displaced = raw_root / "upstream" / "owned-displaced"
            external = root / "external-checkout"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                first_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            run_git(root, "clone", str(fixture.repository), str(external))
            run_git(external, "checkout", "--detach", fixture.first_commit)
            external_head = run_git(external, "rev-parse", "HEAD").stdout.strip()
            real_run_anchored_git = fetch_upstream.run_anchored_git
            substituted = False

            def substitute_before_checkout(
                arguments: list[str],
                checkout_fd: int,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                nonlocal substituted
                if (
                    not substituted
                    and arguments[:3] == ["git", "checkout", "--detach"]
                ):
                    checkout.rename(displaced)
                    checkout.symlink_to(external, target_is_directory=True)
                    substituted = True
                return real_run_anchored_git(
                    arguments,
                    checkout_fd,
                    timeout_seconds=timeout_seconds,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_anchored_git",
                side_effect=substitute_before_checkout,
            ):
                second_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.second_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, first_exit)
            self.assertEqual(1, second_exit)
            self.assertTrue(substituted)
            self.assertEqual(
                external_head,
                run_git(external, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                fixture.first_commit,
                run_git(displaced, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertFalse(report_path.exists())

    def test_git_directory_substitution_cannot_reach_external_repository(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-gitdir-race-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            external = root / "external-checkout"
            displaced_git = checkout / ".git-owned"
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                first_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            run_git(root, "clone", str(fixture.repository), str(external))
            run_git(external, "checkout", "--detach", fixture.first_commit)
            external_head = run_git(external, "rev-parse", "HEAD").stdout.strip()
            external_config = (external / ".git" / "config").read_bytes()
            external_worktree = (external / CORE_PATH).read_bytes()
            real_run_anchored_git = fetch_upstream.run_anchored_git
            substituted = False

            def substitute_git_directory_before_checkout(
                arguments: list[str],
                context: int | fetch_upstream.GitMetadataContext,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                nonlocal substituted
                if (
                    not substituted
                    and arguments[:3] == ["git", "checkout", "--detach"]
                ):
                    (checkout / ".git").rename(displaced_git)
                    (checkout / ".git").symlink_to(
                        external / ".git",
                        target_is_directory=True,
                    )
                    substituted = True
                return real_run_anchored_git(
                    arguments,
                    context,
                    timeout_seconds=timeout_seconds,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_anchored_git",
                side_effect=substitute_git_directory_before_checkout,
            ):
                second_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.second_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            owned_head = subprocess.run(
                [
                    "git",
                    f"--git-dir={displaced_git}",
                    f"--work-tree={checkout}",
                    "rev-parse",
                    "HEAD",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
            self.assertEqual(0, first_exit)
            self.assertEqual(1, second_exit)
            self.assertTrue(substituted)
            self.assertEqual(fixture.first_commit, owned_head)
            self.assertEqual(
                external_head,
                run_git(external, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(external_config, (external / ".git" / "config").read_bytes())
            self.assertEqual(external_worktree, (external / CORE_PATH).read_bytes())
            self.assertFalse(report_path.exists())

    def test_git_directory_substitution_during_initialization_fails_closed(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-gitdir-init-race-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            external = root / "external-checkout"
            displaced_git = checkout / ".git-owned"
            run_git(root, "clone", str(fixture.repository), str(external))
            external_head = run_git(external, "rev-parse", "HEAD").stdout.strip()
            external_config = (external / ".git" / "config").read_bytes()
            external_worktree = (external / CORE_PATH).read_bytes()
            fixture.write_first_lock(lock_path)
            real_run_anchored_git = fetch_upstream.run_anchored_git
            substituted = False

            def substitute_git_directory_before_init(
                arguments: list[str],
                context: int | fetch_upstream.GitMetadataContext,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                nonlocal substituted
                if not substituted and arguments == ["git", "init"]:
                    self.assertIsInstance(
                        context,
                        fetch_upstream.GitMetadataContext,
                    )
                    (checkout / ".git").rename(displaced_git)
                    (checkout / ".git").symlink_to(
                        external / ".git",
                        target_is_directory=True,
                    )
                    substituted = True
                return real_run_anchored_git(
                    arguments,
                    context,
                    timeout_seconds=timeout_seconds,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_anchored_git",
                side_effect=substitute_git_directory_before_init,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertTrue(substituted)
            self.assertTrue(displaced_git.is_dir())
            self.assertEqual(
                external_head,
                run_git(external, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertEqual(
                external_config,
                (external / ".git" / "config").read_bytes(),
            )
            self.assertEqual(external_worktree, (external / CORE_PATH).read_bytes())
            self.assertFalse(report_path.exists())

    def test_inventory_path_substitution_cannot_supply_external_bytes(self) -> None:
        with TemporaryDirectory(prefix="pinned-inventory-race-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            displaced = raw_root / "upstream" / "owned-displaced"
            external = root / "external-checkout"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                first_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            run_git(root, "clone", str(fixture.repository), str(external))
            external_file = external / CORE_PATH
            external_file.write_text("external inventory bytes\n", encoding="utf-8")
            real_run_anchored_git_bytes = fetch_upstream.run_anchored_git_bytes
            substituted = False

            def substitute_during_inventory(
                arguments: list[str],
                checkout_fd: int,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[bytes]:
                nonlocal substituted
                if not substituted and "ls-tree" in arguments:
                    checkout.rename(displaced)
                    checkout.symlink_to(external, target_is_directory=True)
                    substituted = True
                return real_run_anchored_git_bytes(
                    arguments,
                    checkout_fd,
                    timeout_seconds=timeout_seconds,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_anchored_git_bytes",
                side_effect=substitute_during_inventory,
            ):
                second_exit = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--offline",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, first_exit)
            self.assertEqual(1, second_exit)
            self.assertTrue(substituted)
            self.assertEqual(
                "external inventory bytes\n",
                external_file.read_text(encoding="utf-8"),
            )
            self.assertFalse(report_path.exists())

    def test_refresh_fetches_only_requested_commit_and_detaches_head(self) -> None:
        with TemporaryDirectory(prefix="pinned-refresh-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            lock_path = root / "locks" / "upstream.yaml"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            fixture.write_first_lock(lock_path)
            real_run_anchored_git = fetch_upstream.run_anchored_git
            calls: list[tuple[list[str], int]] = []
            descriptor_first_initializations: list[bool] = []

            def recording_run_anchored_git(
                arguments: list[str],
                checkout_fd: int,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                calls.append((arguments, timeout_seconds))
                if arguments == ["git", "init"]:
                    descriptor_first_initializations.append(
                        isinstance(
                            checkout_fd,
                            fetch_upstream.GitMetadataContext,
                        )
                        and (checkout / ".git").is_dir()
                    )
                return real_run_anchored_git(
                    arguments,
                    checkout_fd,
                    timeout_seconds=timeout_seconds,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_anchored_git",
                side_effect=recording_run_anchored_git,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertEqual(
                fixture.first_commit,
                run_git(checkout, "rev-parse", "HEAD").stdout.strip(),
            )
            detached = subprocess.run(
                ["git", "-C", str(checkout), "symbolic-ref", "-q", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(1, detached.returncode)
            self.assertEqual(
                fixture.first_files[CORE_PATH],
                (checkout / CORE_PATH).read_text(encoding="utf-8"),
            )
            self.assertEqual(
                fetch_upstream.CHECKOUT_OWNER_CONTENT,
                (
                    checkout
                    / ".git"
                    / fetch_upstream.CHECKOUT_OWNER_MARKER
                ).read_text(encoding="utf-8"),
            )

            command_words = [word for arguments, _ in calls for word in arguments]
            self.assertNotIn("pull", command_words)
            fetch_calls = [
                (arguments, timeout)
                for arguments, timeout in calls
                if "fetch" in arguments
            ]
            self.assertEqual(1, len(fetch_calls))
            self.assertIn(fixture.first_commit, fetch_calls[0][0])
            self.assertEqual(
                fetch_upstream.NETWORK_GIT_TIMEOUT_SECONDS,
                fetch_calls[0][1],
            )
            self.assertTrue(all(timeout > 0 for _, timeout in calls))
            self.assertEqual([True], descriptor_first_initializations)

            report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
            self.assertEqual(fetch_upstream.REPORT_OWNER, report["report_owner"])
            self.assertEqual(fixture.first_commit, report["repository"]["resolved_commit"])
            self.assertEqual(
                {"added": 0, "removed": 0, "changed": 0, "unchanged": 4},
                report["summary"],
            )
            candidate_paths = {
                item["path"]
                for items in report["candidate_inventory"].values()
                for item in items
            }
            self.assertIn(str(NESTED_CORE_PATH), candidate_paths)
            self.assertFalse(report["promotion"]["performed"])

    def test_comparison_is_review_only_and_preserves_curated_state(self) -> None:
        with TemporaryDirectory(prefix="pinned-comparison-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            lock_path = root / "reviewed" / "locks" / "upstream.yaml"
            content_path = root / "reviewed" / "content" / "core" / "basics.yaml"
            manifest_path = root / "reviewed" / "manifests" / "topic-map.yaml"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            fixture.write_first_lock(lock_path)
            content_path.parent.mkdir(parents=True)
            content_path.write_text("slug: basics\n", encoding="utf-8")
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("role: provenance-lock-index\n", encoding="utf-8")
            reviewed_paths = (lock_path, content_path, manifest_path)
            before = {path: path.read_bytes() for path in reviewed_paths}

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.second_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertEqual(before, {path: path.read_bytes() for path in reviewed_paths})
            report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {"added": 1, "removed": 0, "changed": 1, "unchanged": 3},
                report["summary"],
            )
            self.assertEqual(
                [str(ADDED_PACKAGE_PATH)],
                [item["path"] for item in report["changes"]["added"]],
            )
            self.assertEqual(
                [str(CORE_PATH)],
                [item["path"] for item in report["changes"]["changed"]],
            )
            self.assertFalse(report["promotion"]["performed"])

    def test_report_cannot_overwrite_a_reviewed_path(self) -> None:
        with TemporaryDirectory(prefix="pinned-isolation-") as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            reviewed_path = root / "locks" / "upstream.yaml"
            reviewed_path.parent.mkdir()
            reviewed_path.write_text("reviewed: true\n", encoding="utf-8")

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=raw_root / "upstream" / "stata-skill",
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--offline",
                        "--report",
                        str(reviewed_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                "reviewed: true\n",
                reviewed_path.read_text(encoding="utf-8"),
            )
            self.assertFalse(raw_root.exists())

    def test_report_cannot_target_checkout_metadata(self) -> None:
        with TemporaryDirectory(prefix="pinned-report-metadata-") as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            config_path = checkout / ".git" / "config"
            config_path.parent.mkdir(parents=True)
            config_path.write_bytes(b"foreign git metadata\n")

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--offline",
                        "--report",
                        str(config_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(b"foreign git metadata\n", config_path.read_bytes())
            self.assertFalse((raw_root / "candidates").exists())

    def test_foreign_canonical_report_is_preserved_before_checkout_creation(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-report-foreign-") as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            report_path.parent.mkdir(parents=True)
            report_path.write_bytes(b"foreign: preserve exactly\n")

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertEqual(
                b"foreign: preserve exactly\n",
                report_path.read_bytes(),
            )
            self.assertFalse(checkout.exists())

    def test_oversized_canonical_report_is_rejected_before_hashing(self) -> None:
        with TemporaryDirectory(prefix="pinned-report-oversized-") as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            report_path.parent.mkdir(parents=True)
            with report_path.open("wb") as handle:
                handle.truncate(fetch_upstream.MAX_REPORT_BYTES + 1)
            report_before = report_path.stat()

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
            ), patch.object(
                fetch_upstream.hashlib,
                "sha256",
                side_effect=AssertionError("oversized report must not be hashed"),
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--report",
                        str(report_path),
                    ]
                )

            report_after = report_path.stat()
            self.assertEqual(1, exit_code)
            self.assertEqual(report_before.st_ino, report_after.st_ino)
            self.assertEqual(report_before.st_size, report_after.st_size)
            self.assertFalse(checkout.exists())

    def test_legacy_canonical_report_requires_explicit_recovery(self) -> None:
        with TemporaryDirectory(prefix="pinned-report-legacy-") as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            report_path.parent.mkdir(parents=True)
            legacy = "report_type: upstream-comparison\nschema_version: 1\n"
            report_path.write_text(legacy, encoding="utf-8")

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
            ), patch("builtins.print") as mocked_print:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--report",
                        str(report_path),
                    ]
                )

            output = "\n".join(
                str(call.args[0]) for call in mocked_print.call_args_list
            )
            self.assertEqual(1, exit_code)
            self.assertIn("Legacy comparison report lacks report_owner", output)
            self.assertIn("move or delete", output)
            self.assertEqual(legacy, report_path.read_text(encoding="utf-8"))
            self.assertFalse(checkout.exists())

    def test_foreign_report_substituted_during_quarantine_is_restored(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-report-quarantine-race-") as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            displaced_owned = report_path.parent / ".owned-report-displaced"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                "schema_version: 1\n"
                "report_type: upstream-comparison\n"
                f"report_owner: {fetch_upstream.REPORT_OWNER}\n",
                encoding="utf-8",
            )
            real_rename = fetch_upstream.atomic_rename_at_no_replace
            substituted = False

            def substitute_before_quarantine_move(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal substituted
                if (
                    not substituted
                    and source_name == report_path.name
                    and destination_name.endswith(".stale")
                ):
                    fetch_upstream.os.rename(
                        source_name,
                        displaced_owned.name,
                        src_dir_fd=source_descriptor,
                        dst_dir_fd=source_descriptor,
                    )
                    foreign_fd = fetch_upstream.os.open(
                        source_name,
                        fetch_upstream.os.O_WRONLY
                        | fetch_upstream.os.O_CREAT
                        | fetch_upstream.os.O_EXCL,
                        0o600,
                        dir_fd=source_descriptor,
                    )
                    with fetch_upstream.os.fdopen(
                        foreign_fd,
                        "wb",
                    ) as handle:
                        handle.write(b"foreign replacement: preserve\n")
                    substituted = True
                real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
            ), patch.object(
                fetch_upstream,
                "atomic_rename_at_no_replace",
                side_effect=substitute_before_quarantine_move,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertTrue(substituted)
            self.assertEqual(
                b"foreign replacement: preserve\n",
                report_path.read_bytes(),
            )
            self.assertTrue(displaced_owned.is_file())
            self.assertFalse(checkout.exists())

    def test_quarantine_recovery_is_parent_identity_aware_after_displacement(
        self,
    ) -> None:
        for create_restore_conflict in (False, True):
            with self.subTest(
                create_restore_conflict=create_restore_conflict
            ), TemporaryDirectory(
                prefix="pinned-report-parent-displacement-"
            ) as temp_root:
                root = Path(temp_root)
                raw_root = root / "raw"
                report_path = (
                    raw_root / "candidates" / "upstream-comparison.yaml"
                )
                displaced_parent = raw_root / "candidates-displaced"
                report_path.parent.mkdir(parents=True)
                report_bytes = (
                    "schema_version: 1\n"
                    "report_type: upstream-comparison\n"
                    f"report_owner: {fetch_upstream.REPORT_OWNER}\n"
                ).encode("utf-8")
                report_path.write_bytes(report_bytes)
                with patch.multiple(
                    fetch_upstream,
                    REPO_ROOT=root,
                    RAW_ROOT=raw_root,
                    UPSTREAM_REPO_DIR=(
                        raw_root / "upstream" / "stata-skill"
                    ),
                ):
                    target = fetch_upstream.open_report_target(report_path)
                    displaced = False

                    def displace_parent_then_fail_verification(
                        candidate_target: fetch_upstream.ReportTarget,
                        entry_name: str,
                        owner_descriptor: int,
                        expected: fetch_upstream.TemporaryFileState,
                    ) -> None:
                        del entry_name, owner_descriptor, expected
                        nonlocal displaced
                        self.assertIs(candidate_target, target)
                        report_path.parent.rename(displaced_parent)
                        report_path.parent.mkdir()
                        if create_restore_conflict:
                            conflict_fd = fetch_upstream.os.open(
                                target.name,
                                fetch_upstream.os.O_WRONLY
                                | fetch_upstream.os.O_CREAT
                                | fetch_upstream.os.O_EXCL,
                                0o600,
                                dir_fd=target.parent_fd,
                            )
                            with fetch_upstream.os.fdopen(
                                conflict_fd,
                                "wb",
                            ) as handle:
                                handle.write(b"foreign restore conflict\n")
                        displaced = True
                        raise RuntimeError("forced verification failure")

                    try:
                        with patch.object(
                            fetch_upstream,
                            "verify_owned_temporary_entry",
                            side_effect=displace_parent_then_fail_verification,
                        ), self.assertRaises(RuntimeError) as raised:
                            fetch_upstream.remove_stale_report(target)
                    finally:
                        target.close()

                self.assertTrue(displaced)
                self.assertFalse(report_path.exists())
                message = str(raised.exception)
                self.assertIn("descriptor-held displaced report directory", message)
                self.assertIn("no verified current pathname", message)
                self.assertNotIn(
                    f"survives unchanged at {report_path}",
                    message,
                )
                if create_restore_conflict:
                    self.assertEqual(
                        b"foreign restore conflict\n",
                        (displaced_parent / report_path.name).read_bytes(),
                    )
                    quarantines = list(
                        displaced_parent.glob(f".{report_path.name}.*.stale")
                    )
                    self.assertEqual(1, len(quarantines))
                    self.assertEqual(report_bytes, quarantines[0].read_bytes())
                else:
                    self.assertEqual(
                        report_bytes,
                        (displaced_parent / report_path.name).read_bytes(),
                    )

    def test_quarantine_recovery_handles_an_absent_public_parent(self) -> None:
        with TemporaryDirectory(
            prefix="pinned-report-parent-absent-"
        ) as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            report_path = (
                raw_root / "candidates" / "upstream-comparison.yaml"
            )
            displaced_parent = raw_root / "candidates-displaced"
            report_path.parent.mkdir(parents=True)
            report_bytes = (
                "schema_version: 1\n"
                "report_type: upstream-comparison\n"
                f"report_owner: {fetch_upstream.REPORT_OWNER}\n"
            ).encode("utf-8")
            report_path.write_bytes(report_bytes)
            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=raw_root / "upstream" / "stata-skill",
            ):
                target = fetch_upstream.open_report_target(report_path)

                def displace_parent_then_fail_verification(
                    candidate_target: fetch_upstream.ReportTarget,
                    entry_name: str,
                    owner_descriptor: int,
                    expected: fetch_upstream.TemporaryFileState,
                ) -> None:
                    del entry_name, owner_descriptor, expected
                    self.assertIs(candidate_target, target)
                    report_path.parent.rename(displaced_parent)
                    raise RuntimeError("forced verification failure")

                try:
                    with patch.object(
                        fetch_upstream,
                        "verify_owned_temporary_entry",
                        side_effect=displace_parent_then_fail_verification,
                    ), self.assertRaises(RuntimeError) as raised:
                        fetch_upstream.remove_stale_report(target)
                finally:
                    target.close()

            message = str(raised.exception)
            self.assertIn("descriptor-held displaced report directory", message)
            self.assertIn("current pathname unknown", message)
            self.assertIn(f"basename {report_path.name!r}", message)
            self.assertIn("device=", message)
            self.assertIn("inode=", message)
            self.assertEqual(
                report_bytes,
                (displaced_parent / report_path.name).read_bytes(),
            )

    def test_unknown_candidate_state_does_not_report_a_former_public_path(
        self,
    ) -> None:
        with TemporaryDirectory(
            prefix="pinned-report-unknown-state-"
        ) as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            report_path = (
                raw_root / "candidates" / "upstream-comparison.yaml"
            )
            displaced_parent = raw_root / "candidates-displaced"
            report_path.parent.mkdir(parents=True)
            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=raw_root / "upstream" / "stata-skill",
            ):
                target = fetch_upstream.open_report_target(report_path)

                def displace_before_state_capture(
                    file_descriptor: int,
                    *,
                    maximum_size: int | None = None,
                ) -> fetch_upstream.TemporaryFileState:
                    del file_descriptor, maximum_size
                    report_path.parent.rename(displaced_parent)
                    raise OSError("forced initial state-capture failure")

                try:
                    with patch.object(
                        fetch_upstream,
                        "temporary_file_state",
                        side_effect=displace_before_state_capture,
                    ), self.assertRaises(RuntimeError) as raised:
                        fetch_upstream.write_report_atomically(
                            target,
                            {"schema_version": 1},
                        )
                finally:
                    target.close()

            candidates = list(
                displaced_parent.glob(f".{report_path.name}.*.tmp")
            )
            self.assertEqual(1, len(candidates))
            former_path = report_path.parent / candidates[0].name
            message = str(raised.exception)
            self.assertIn("candidate state could not be fully verified", message)
            self.assertIn("descriptor-held displaced report directory", message)
            self.assertIn("current pathname unknown", message)
            self.assertIn(f"basename {candidates[0].name!r}", message)
            self.assertIn("device=", message)
            self.assertIn("inode=", message)
            self.assertNotIn(str(former_path), message)

    def test_unknown_candidate_state_does_not_name_a_substituted_entry(
        self,
    ) -> None:
        with TemporaryDirectory(
            prefix="pinned-report-unknown-substitution-"
        ) as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            report_path = (
                raw_root / "candidates" / "upstream-comparison.yaml"
            )
            report_path.parent.mkdir(parents=True)
            external = root / "external-candidate-bytes"
            sentinel = b"external candidate sentinel\n"
            external.write_bytes(sentinel)
            candidate_path: Path | None = None
            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=raw_root / "upstream" / "stata-skill",
            ):
                target = fetch_upstream.open_report_target(report_path)

                def substitute_before_state_capture(
                    file_descriptor: int,
                    *,
                    maximum_size: int | None = None,
                ) -> fetch_upstream.TemporaryFileState:
                    nonlocal candidate_path
                    del file_descriptor, maximum_size
                    candidates = list(
                        report_path.parent.glob(f".{report_path.name}.*.tmp")
                    )
                    self.assertEqual(1, len(candidates))
                    candidate_path = candidates[0]
                    candidate_path.unlink()
                    os.link(external, candidate_path)
                    raise OSError("forced initial state-capture failure")

                try:
                    with patch.object(
                        fetch_upstream,
                        "temporary_file_state",
                        side_effect=substitute_before_state_capture,
                    ), self.assertRaises(RuntimeError) as raised:
                        fetch_upstream.write_report_atomically(
                            target,
                            {"schema_version": 1},
                        )
                finally:
                    target.close()

            self.assertIsNotNone(candidate_path)
            assert candidate_path is not None
            message = str(raised.exception)
            self.assertIn("candidate state could not be fully verified", message)
            self.assertIn("descriptor-held report directory", message)
            self.assertIn("no verified current pathname", message)
            self.assertIn(f"basename {candidate_path.name!r}", message)
            self.assertNotIn(str(candidate_path), message)
            self.assertEqual(sentinel, external.read_bytes())
            self.assertEqual(sentinel, candidate_path.read_bytes())
            self.assertFalse(report_path.exists())

    def test_fetch_timeout_fails_without_writing_a_report(self) -> None:
        with TemporaryDirectory(prefix="pinned-timeout-") as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            lock_path.parent.mkdir()
            lock_path.write_text(
                "schema_version: 1\n"
                "repository:\n"
                "  url: https://example.invalid/upstream.git\n"
                f"  commit: {'a' * 40}\n"
                f"  expected_commit: {'a' * 40}\n"
                "files: {}\n",
                encoding="utf-8",
            )
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                "schema_version: 1\n"
                "report_type: upstream-comparison\n"
                f"report_owner: {fetch_upstream.REPORT_OWNER}\n",
                encoding="utf-8",
            )
            calls: list[tuple[list[str], int]] = []

            def timed_out(
                arguments: list[str],
                checkout_fd: int,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                del checkout_fd
                calls.append((arguments, timeout_seconds))
                return CompletedProcess(
                    arguments,
                    124,
                    "",
                    f"Command timed out after {timeout_seconds} seconds.",
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL="https://example.invalid/upstream.git",
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_anchored_git",
                side_effect=timed_out,
            ), patch("builtins.print") as mocked_print:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        "a" * 40,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertFalse(report_path.exists())
            quarantines = list(
                report_path.parent.glob(f".{report_path.name}.*.stale")
            )
            self.assertEqual(1, len(quarantines))
            self.assertIn(
                fetch_upstream.REPORT_OWNER,
                quarantines[0].read_text(encoding="utf-8"),
            )
            self.assertEqual(1, len(calls))
            self.assertEqual(
                fetch_upstream.LOCAL_GIT_TIMEOUT_SECONDS,
                calls[0][1],
            )
            output = "\n".join(
                str(call.args[0]) for call in mocked_print.call_args_list
            )
            self.assertIn(str(quarantines[0]), output)
            self.assertIn("survives unchanged", output)

    def test_stale_report_quarantine_failure_reports_exact_surviving_path(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-stale-report-fsync-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            fixture.write_first_lock(lock_path)
            report_path.parent.mkdir(parents=True)
            report_bytes = (
                "schema_version: 1\n"
                "report_type: upstream-comparison\n"
                f"report_owner: {fetch_upstream.REPORT_OWNER}\n"
            ).encode("utf-8")
            report_path.write_bytes(report_bytes)
            real_fsync = fetch_upstream.os.fsync

            def fail_quarantine_fsync(file_descriptor: int) -> None:
                quarantines = list(
                    report_path.parent.glob(f".{report_path.name}.*.stale")
                )
                if quarantines and not report_path.exists():
                    raise OSError("forced stale-report directory fsync failure")
                real_fsync(file_descriptor)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.os,
                "fsync",
                side_effect=fail_quarantine_fsync,
            ), patch.object(
                fetch_upstream.subprocess,
                "Popen",
            ) as mocked_popen, patch("builtins.print") as mocked_print:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            quarantines = list(
                report_path.parent.glob(f".{report_path.name}.*.stale")
            )
            output = "\n".join(
                str(call.args[0]) for call in mocked_print.call_args_list
            )
            self.assertEqual(1, exit_code)
            mocked_popen.assert_not_called()
            self.assertFalse(report_path.exists())
            self.assertEqual(1, len(quarantines))
            self.assertEqual(report_bytes, quarantines[0].read_bytes())
            self.assertIn(str(quarantines[0]), output)
            self.assertIn("survives unchanged", output)

    def test_report_swap_failure_preserves_owned_candidate_without_unlink(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-report-swap-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            lock_path = root / "locks" / "upstream.yaml"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            fixture.write_first_lock(lock_path)
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                "schema_version: 1\n"
                "report_type: upstream-comparison\n"
                f"report_owner: {fetch_upstream.REPORT_OWNER}\n",
                encoding="utf-8",
            )
            real_rename = fetch_upstream.atomic_rename_at_no_replace

            def fail_report_publication(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                if source_name.endswith(".tmp"):
                    raise OSError("forced report swap failure")
                real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "atomic_rename_at_no_replace",
                side_effect=fail_report_publication,
            ), patch.object(
                fetch_upstream.os,
                "unlink",
                wraps=os.unlink,
            ) as unlink:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertFalse(report_path.exists())
            unlink.assert_not_called()
            candidates = list(
                report_path.parent.glob(f".{report_path.name}.*.tmp")
            )
            self.assertEqual(1, len(candidates))
            self.assertIn(
                fetch_upstream.REPORT_OWNER,
                candidates[0].read_text(encoding="utf-8"),
            )

    def test_post_rename_failure_reports_public_surviving_path(self) -> None:
        with TemporaryDirectory(prefix="pinned-report-post-rename-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            lock_path = root / "locks" / "upstream.yaml"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            fixture.write_first_lock(lock_path)
            real_fsync = fetch_upstream.os.fsync

            def fail_public_report_fsync(file_descriptor: int) -> None:
                if report_path.exists():
                    raise OSError("forced final report fsync failure")
                real_fsync(file_descriptor)

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.os,
                "fsync",
                side_effect=fail_public_report_fsync,
            ), patch("builtins.print") as mocked_print:
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            output = "\n".join(
                str(call.args[0]) for call in mocked_print.call_args_list
            )
            self.assertEqual(1, exit_code)
            self.assertTrue(report_path.is_file(), output)
            self.assertIn(str(report_path), output)
            self.assertIn("survives unchanged", output)
            self.assertNotIn("preserved failed candidate as .", output)

    def test_concurrent_report_is_preserved_by_no_replace_publication(self) -> None:
        with TemporaryDirectory(prefix="pinned-report-concurrent-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            lock_path = root / "locks" / "upstream.yaml"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            fixture.write_first_lock(lock_path)
            real_rename = fetch_upstream.atomic_rename_at_no_replace
            inserted = False

            def insert_concurrent_report(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal inserted
                if not inserted:
                    concurrent_fd = fetch_upstream.os.open(
                        destination_name,
                        fetch_upstream.os.O_WRONLY
                        | fetch_upstream.os.O_CREAT
                        | fetch_upstream.os.O_EXCL,
                        0o600,
                        dir_fd=destination_descriptor,
                    )
                    with fetch_upstream.os.fdopen(
                        concurrent_fd,
                        "w",
                        encoding="utf-8",
                    ) as handle:
                        handle.write("concurrent: preserve\n")
                    inserted = True
                real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "atomic_rename_at_no_replace",
                side_effect=insert_concurrent_report,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(1, exit_code)
            self.assertTrue(inserted)
            self.assertEqual(
                "concurrent: preserve\n",
                report_path.read_text(encoding="utf-8"),
            )
            candidates = list(
                report_path.parent.glob(f".{report_path.name}.*.tmp")
            )
            self.assertEqual(1, len(candidates))
            self.assertIn(
                fetch_upstream.REPORT_OWNER,
                candidates[0].read_text(encoding="utf-8"),
            )

    def test_substituted_temporary_report_is_preserved_and_not_published(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="pinned-report-temp-race-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            lock_path = root / "locks" / "upstream.yaml"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            fixture.write_first_lock(lock_path)
            real_verify = fetch_upstream.verify_owned_temporary_entry
            substituted = False
            verification_calls = 0

            def substitute_before_verification(
                target: fetch_upstream.ReportTarget,
                entry_name: str,
                owner_descriptor: int,
                expected: fetch_upstream.TemporaryFileState,
            ) -> None:
                nonlocal substituted, verification_calls
                verification_calls += 1
                if not substituted and entry_name.startswith("."):
                    fetch_upstream.os.unlink(
                        entry_name,
                        dir_fd=target.parent_fd,
                    )
                    foreign_fd = fetch_upstream.os.open(
                        entry_name,
                        fetch_upstream.os.O_WRONLY
                        | fetch_upstream.os.O_CREAT
                        | fetch_upstream.os.O_EXCL,
                        0o600,
                        dir_fd=target.parent_fd,
                    )
                    with fetch_upstream.os.fdopen(
                        foreign_fd,
                        "w",
                        encoding="utf-8",
                    ) as handle:
                        handle.write("foreign temporary bytes\n")
                    substituted = True
                real_verify(
                    target,
                    entry_name,
                    owner_descriptor,
                    expected,
                )

            with patch.multiple(
                fetch_upstream,
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "verify_owned_temporary_entry",
                side_effect=substitute_before_verification,
            ):
                exit_code = fetch_upstream.main(
                    [
                        "--upstream-ref",
                        fixture.first_commit,
                        "--report",
                        str(report_path),
                    ]
                )

            temporary_files = list(
                report_path.parent.glob(f".{report_path.name}.*.tmp")
            )
            self.assertEqual(1, exit_code)
            self.assertTrue(substituted)
            self.assertEqual(1, verification_calls)
            self.assertFalse(report_path.exists())
            self.assertEqual(1, len(temporary_files))
            self.assertEqual(
                "foreign temporary bytes\n",
                temporary_files[0].read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
