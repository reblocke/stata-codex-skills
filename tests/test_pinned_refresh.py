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
IGNORED_COLLISION_PATH = Path("local-cache.txt")


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

    def test_symlinked_raw_root_cannot_replace_an_external_report(self) -> None:
        with TemporaryDirectory(prefix="pinned-report-symlink-") as temp_root:
            root = Path(temp_root)
            repository_root = root / "repository"
            external_root = root / "external"
            repository_root.mkdir()
            report_path = repository_root / "raw" / "candidates" / "report.yaml"
            external_report = external_root / "candidates" / "report.yaml"
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
            report_path = raw_root / "candidates" / "report.yaml"
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
            report_path = raw_root / "candidates" / "report.yaml"

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
            report_path = raw_root / "candidates" / "report.yaml"
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
            report_path = raw_root / "candidates" / "report.yaml"
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

    def test_wrong_origin_unmarked_checkout_is_not_adopted(self) -> None:
        with TemporaryDirectory(prefix="pinned-origin-adoption-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            repository_root = root / "repository"
            raw_root = repository_root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "report.yaml"
            lock_path = repository_root / "locks" / "upstream.yaml"
            checkout.parent.mkdir(parents=True)
            run_git(root, "clone", str(fixture.repository), str(checkout))
            original_head = run_git(checkout, "rev-parse", "HEAD").stdout.strip()
            fixture.write_first_lock(lock_path)

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

    def test_dirty_owned_checkout_is_not_replaced(self) -> None:
        with TemporaryDirectory(prefix="pinned-dirty-checkout-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            report_path = raw_root / "candidates" / "report.yaml"
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
            report_path = raw_root / "candidates" / "report.yaml"
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
            report_path = raw_root / "candidates" / "report.yaml"
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
            report_path = raw_root / "candidates" / "report.yaml"
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
                fixture.second_commit,
                run_git(displaced, "rev-parse", "HEAD").stdout.strip(),
            )
            self.assertFalse(report_path.exists())

    def test_inventory_path_substitution_cannot_supply_external_bytes(self) -> None:
        with TemporaryDirectory(prefix="pinned-inventory-race-") as temp_root:
            root = Path(temp_root)
            fixture = UpstreamFixture(root)
            raw_root = root / "raw"
            checkout = raw_root / "upstream" / "stata-skill"
            displaced = raw_root / "upstream" / "owned-displaced"
            external = root / "external-checkout"
            report_path = raw_root / "candidates" / "report.yaml"
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
                if not substituted and arguments[:3] == ["git", "ls-tree", "-r"]:
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

            def recording_run_anchored_git(
                arguments: list[str],
                checkout_fd: int,
                timeout_seconds: int = 120,
            ) -> CompletedProcess[str]:
                calls.append((arguments, timeout_seconds))
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
                REPO_ROOT=root,
                RAW_ROOT=raw_root,
                UPSTREAM_REPO_DIR=checkout,
                UPSTREAM_REPO_URL=str(fixture.repository),
                UPSTREAM_LOCK_PATH=lock_path,
            ), patch.object(
                fetch_upstream,
                "atomic_rename_at_no_replace",
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
            self.assertEqual(
                [],
                list(report_path.parent.glob(f".{report_path.name}.*.tmp")),
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
            self.assertGreaterEqual(verification_calls, 2)
            self.assertFalse(report_path.exists())
            self.assertEqual(1, len(temporary_files))
            self.assertEqual(
                "foreign temporary bytes\n",
                temporary_files[0].read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
