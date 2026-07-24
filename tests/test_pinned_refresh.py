from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import subprocess
import sys
import unittest
from unittest.mock import patch

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


def run_git(repository: Path, *arguments: str) -> CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
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
        run_git(self.repository, "add", ".")
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

    def test_refresh_fetches_only_requested_commit_and_detaches_head(self) -> None:
        with TemporaryDirectory(prefix="pinned-refresh-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            lock_path = root / "locks" / "upstream.yaml"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            fixture.write_first_lock(lock_path)
            real_run_command = fetch_upstream.run_command
            calls: list[tuple[list[str], int]] = []

            def recording_run_command(
                arguments: list[str],
                cwd: Path | None = None,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                calls.append((arguments, timeout_seconds))
                return real_run_command(
                    arguments,
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                )

            with patch.multiple(
                fetch_upstream,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_command",
                side_effect=recording_run_command,
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

            report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
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

    def test_fetch_timeout_fails_without_writing_a_report(self) -> None:
        with TemporaryDirectory(prefix="pinned-timeout-") as temp_root:
            root = Path(temp_root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            lock_path = root / "locks" / "upstream.yaml"
            lock_path.parent.mkdir()
            lock_path.write_text("repository: {}\nfiles: {}\n", encoding="utf-8")
            report_path.parent.mkdir(parents=True)
            report_path.write_text("stale: true\n", encoding="utf-8")
            calls: list[tuple[list[str], int]] = []

            def timed_out(
                arguments: list[str],
                cwd: Path | None = None,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                del cwd
                calls.append((arguments, timeout_seconds))
                return CompletedProcess(
                    arguments,
                    124,
                    "",
                    f"Command timed out after {timeout_seconds} seconds.",
                )

            with patch.multiple(
                fetch_upstream,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL="https://example.invalid/upstream.git",
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "run_command",
                side_effect=timed_out,
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
            self.assertFalse(report_path.exists())
            self.assertEqual(1, len(calls))
            self.assertEqual(
                fetch_upstream.LOCAL_GIT_TIMEOUT_SECONDS,
                calls[0][1],
            )

    def test_report_swap_failure_leaves_no_stale_or_partial_report(self) -> None:
        with TemporaryDirectory(prefix="pinned-report-swap-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            lock_path = root / "locks" / "upstream.yaml"
            report_path = raw_root / "candidates" / "upstream-comparison.yaml"
            fixture.write_first_lock(lock_path)
            report_path.parent.mkdir(parents=True)
            report_path.write_text("stale: true\n", encoding="utf-8")

            with patch.multiple(
                fetch_upstream,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream.os,
                "replace",
                side_effect=OSError("forced report swap failure"),
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
            self.assertFalse(report_path.exists())
            self.assertEqual(
                [],
                list(report_path.parent.glob(f".{report_path.name}.*.tmp")),
            )


if __name__ == "__main__":
    unittest.main()
