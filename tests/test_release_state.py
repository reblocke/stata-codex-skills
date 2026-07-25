from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_state  # noqa: E402


def run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def initialize_repository(root: Path) -> None:
    run_git(root, "init")
    run_git(root, "config", "user.name", "Release State Test")
    run_git(root, "config", "user.email", "release-state@example.invalid")


def write_complete_tree(root: Path) -> None:
    for folder in release_state.SKILL_FOLDERS:
        skill = root / folder
        (skill / "agents").mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(f"# {folder}\n", encoding="utf-8")
        (skill / "PROVENANCE.md").write_text("# Provenance\n", encoding="utf-8")
        (skill / "agents" / "openai.yaml").write_text(
            f"display_name: {folder}\n",
            encoding="utf-8",
        )


class ReleaseDigestTests(unittest.TestCase):
    def test_source_digest_tracks_review_and_build_inputs(self) -> None:
        with TemporaryDirectory(prefix="release-source-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            source = root / "content" / "core" / "sample.yaml"
            source.parent.mkdir(parents=True)
            source.write_text("slug: sample\n", encoding="utf-8")
            (root / "Makefile").write_text("check:\n\ttrue\n", encoding="utf-8")
            run_git(root, "add", "Makefile", "content/core/sample.yaml")
            first = release_state.source_digest(root)
            source.write_text("slug: changed\n", encoding="utf-8")
            second = release_state.source_digest(root)

        self.assertNotEqual(first, second)

    def test_source_digest_includes_force_tracked_excluded_paths_only(self) -> None:
        with TemporaryDirectory(prefix="release-excluded-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text(
                "raw/\nbuild/\n",
                encoding="utf-8",
            )
            tracked = root / "raw" / "tracked.txt"
            tracked.parent.mkdir()
            tracked.write_text("first\n", encoding="utf-8")
            run_git(root, "add", ".gitignore")
            run_git(root, "add", "-f", "raw/tracked.txt")
            first = release_state.source_digest(root)

            ignored_runtime = root / "raw" / "runtime.log"
            ignored_runtime.write_text("runtime one\n", encoding="utf-8")
            with_ignored_runtime = release_state.source_digest(root)
            (root / "untracked.txt").write_text("not reviewed\n", encoding="utf-8")
            with_untracked_source = release_state.source_digest(root)
            tracked.write_text("second\n", encoding="utf-8")
            second = release_state.source_digest(root)

        self.assertEqual(first, with_ignored_runtime)
        self.assertEqual(first, with_untracked_source)
        self.assertNotEqual(first, second)

    def test_source_digest_is_identical_in_a_linked_worktree(self) -> None:
        with TemporaryDirectory(prefix="release-worktree-") as temp_root:
            root = Path(temp_root)
            repository = root / "repository"
            worktree = root / "worktree"
            repository.mkdir()
            initialize_repository(repository)
            source = repository / "content" / "sample.yaml"
            source.parent.mkdir()
            source.write_text("slug: sample\n", encoding="utf-8")
            run_git(repository, "add", "content/sample.yaml")
            run_git(repository, "commit", "-m", "tracked source")
            run_git(repository, "worktree", "add", "--detach", str(worktree))

            repository_digest = release_state.source_digest(repository)
            worktree_digest = release_state.source_digest(worktree)

        self.assertEqual(repository_digest, worktree_digest)

    def test_source_digest_refuses_symlinked_tracked_ancestor(self) -> None:
        with TemporaryDirectory(prefix="release-symlink-ancestor-") as temp_root:
            root = Path(temp_root)
            repository = root / "repository"
            external = root / "external-content"
            repository.mkdir()
            initialize_repository(repository)
            source = repository / "content" / "sample.yaml"
            source.parent.mkdir()
            source.write_text("slug: tracked\n", encoding="utf-8")
            run_git(repository, "add", "content/sample.yaml")
            source.parent.rename(external)
            source.parent.symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "unsafe ancestor"):
                release_state.source_digest(repository)

            self.assertEqual(
                "slug: tracked\n",
                (external / "sample.yaml").read_text(encoding="utf-8"),
            )

    def test_source_digest_rejects_repository_substitution_during_inventory(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="release-repo-race-") as temp_root:
            root = Path(temp_root)
            repository = root / "repository"
            displaced = root / "repository-displaced"
            external = root / "external"
            repository.mkdir()
            external.mkdir()
            initialize_repository(repository)
            initialize_repository(external)
            source = repository / "content" / "sample.yaml"
            source.parent.mkdir()
            source.write_text("slug: tracked\n", encoding="utf-8")
            run_git(repository, "add", "content/sample.yaml")
            external_source = external / "external.yaml"
            external_source.write_text("external: preserve\n", encoding="utf-8")
            run_git(external, "add", "external.yaml")
            real_run = subprocess.run
            substituted = False

            def substitute_before_git(*args, **kwargs):
                nonlocal substituted
                if not substituted:
                    repository.rename(displaced)
                    repository.symlink_to(external, target_is_directory=True)
                    substituted = True
                return real_run(*args, **kwargs)

            with patch.object(
                release_state.subprocess,
                "run",
                side_effect=substitute_before_git,
            ), self.assertRaisesRegex(ValueError, "changed during source hashing"):
                release_state.source_digest(repository)

            self.assertTrue(substituted)
            self.assertEqual(
                "external: preserve\n",
                external_source.read_text(encoding="utf-8"),
            )

    def test_force_tracked_excluded_file_invalidates_receipt(self) -> None:
        with TemporaryDirectory(prefix="release-excluded-receipt-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text(
                "raw/\nbuild/\n",
                encoding="utf-8",
            )
            tracked = root / "raw" / "tracked.txt"
            tracked.parent.mkdir()
            tracked.write_text("first\n", encoding="utf-8")
            run_git(root, "add", ".gitignore")
            run_git(root, "add", "-f", "raw/tracked.txt")
            build = root / "build" / "generated"
            receipt = root / "build" / "validation-receipt.json"
            write_complete_tree(build)

            real_source_digest = release_state.source_digest
            with patch.object(
                release_state,
                "source_digest",
                side_effect=lambda: real_source_digest(root),
            ):
                release_state.write_validation_receipt(build, receipt)
                tracked.write_text("second\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "source state"):
                    release_state.verify_validation_receipt(build, receipt)

    def test_receipt_rejects_tree_or_source_drift(self) -> None:
        with TemporaryDirectory(prefix="release-receipt-") as temp_root:
            root = Path(temp_root)
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            with patch.object(release_state, "source_digest", return_value="source-a"):
                release_state.write_validation_receipt(build, receipt)
                release_state.verify_validation_receipt(build, receipt)

            (build / "stata-core" / "SKILL.md").write_text(
                "# changed\n",
                encoding="utf-8",
            )
            with patch.object(release_state, "source_digest", return_value="source-a"):
                with self.assertRaisesRegex(ValueError, "build/generated"):
                    release_state.verify_validation_receipt(build, receipt)

            write_complete_tree(build)
            with patch.object(release_state, "source_digest", return_value="source-b"):
                with self.assertRaisesRegex(ValueError, "source state"):
                    release_state.verify_validation_receipt(build, receipt)

    def test_receipt_is_not_written_when_state_changed_during_validation(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="release-receipt-") as temp_root:
            root = Path(temp_root)
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            expected = {
                "source_sha256": "before",
                "tree_sha256": release_state.tree_digest(build),
            }
            with patch.object(
                release_state,
                "source_digest",
                return_value="after",
            ), self.assertRaisesRegex(ValueError, "changed during validation"):
                release_state.write_validation_receipt(
                    build,
                    receipt,
                    expected_state=expected,
                )

        self.assertFalse(receipt.exists())


if __name__ == "__main__":
    unittest.main()
