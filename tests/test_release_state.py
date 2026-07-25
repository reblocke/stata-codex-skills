from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import shutil
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

    def test_source_digest_ignores_ambient_git_configuration(self) -> None:
        with TemporaryDirectory(prefix="release-git-environment-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            source = root / "sample.txt"
            source.write_text("tracked\n", encoding="utf-8")
            run_git(root, "add", "sample.txt")
            expected = release_state.source_digest(root)
            injected = {
                "GIT_CONFIG": "/tmp/foreign-config",
                "GIT_CONFIG_COUNT": "not-a-number",
                "GIT_CONFIG_GLOBAL": "/tmp/foreign-global-config",
                "GIT_CONFIG_KEY_0": "include.path",
                "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/foreign-hooks'",
                "GIT_CONFIG_SYSTEM": "/tmp/foreign-system-config",
                "GIT_CONFIG_VALUE_0": "/tmp/foreign-include",
            }
            with patch.dict(release_state.os.environ, injected, clear=False):
                observed = release_state.source_digest(root)

        self.assertEqual(expected, observed)

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

    def test_source_digest_supports_split_index(self) -> None:
        with TemporaryDirectory(prefix="release-split-index-") as temp_root:
            repository = Path(temp_root)
            initialize_repository(repository)
            source = repository / "content" / "sample.yaml"
            source.parent.mkdir()
            source.write_text("slug: sample\n", encoding="utf-8")
            run_git(repository, "add", "content/sample.yaml")
            ordinary_digest = release_state.source_digest(repository)
            run_git(repository, "update-index", "--split-index")
            split_digest = release_state.source_digest(repository)
            source.write_text("slug: changed\n", encoding="utf-8")
            changed_digest = release_state.source_digest(repository)

        self.assertEqual(ordinary_digest, split_digest)
        self.assertNotEqual(split_digest, changed_digest)

    def test_source_digest_supports_sha256_index(self) -> None:
        with TemporaryDirectory(prefix="release-sha256-index-") as temp_root:
            repository = Path(temp_root)
            initialized = subprocess.run(
                [
                    "git",
                    "init",
                    "--object-format=sha256",
                    str(repository),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if initialized.returncode != 0:
                self.skipTest("installed Git does not support SHA-256 repositories")
            run_git(repository, "config", "user.name", "Release State Test")
            run_git(
                repository,
                "config",
                "user.email",
                "release-state@example.invalid",
            )
            source = repository / "sample.txt"
            source.write_text("first\n", encoding="utf-8")
            run_git(repository, "add", "sample.txt")
            first = release_state.source_digest(repository)
            source.write_text("second\n", encoding="utf-8")
            second = release_state.source_digest(repository)

        self.assertNotEqual(first, second)

    def test_staged_inventory_rejects_sparse_directory_entries(self) -> None:
        payload = (
            b"040000 "
            + b"0" * 40
            + b" 0\tsparse-directory/\0"
        )

        with self.assertRaisesRegex(ValueError, "Sparse Git indexes"):
            release_state._parse_staged_inventory(payload)

    def test_staged_inventory_rejects_unmerged_entries(self) -> None:
        payload = b"100644 " + b"0" * 40 + b" 2\tconflicted.txt\0"

        with self.assertRaisesRegex(ValueError, "unresolved merge stages"):
            release_state._parse_staged_inventory(payload)

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

    def test_source_digest_rejects_tracked_file_mutation_during_read(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="release-file-race-") as temp_root:
            repository = Path(temp_root)
            initialize_repository(repository)
            source = repository / "sample.txt"
            source.write_text("validated bytes\n", encoding="utf-8")
            run_git(repository, "add", "sample.txt")
            real_read = release_state._read_descriptor_bytes
            mutated = False

            def mutate_after_read(file_descriptor: int) -> bytes:
                nonlocal mutated
                payload = real_read(file_descriptor)
                if not mutated:
                    source.write_text(
                        "concurrent replacement bytes\n",
                        encoding="utf-8",
                    )
                    mutated = True
                return payload

            with patch.object(
                release_state,
                "_read_descriptor_bytes",
                side_effect=mutate_after_read,
            ), self.assertRaisesRegex(ValueError, "changed while reading"):
                release_state.source_digest(repository)

            self.assertTrue(mutated)

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

    def test_receipt_binds_file_despite_transient_index_substitution(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="release-index-race-") as temp_root:
            root = Path(temp_root)
            repository = root / "repository"
            repository.mkdir()
            initialize_repository(repository)
            safe = repository / "safe.txt"
            omitted = repository / "omitted.txt"
            safe.write_text("safe\n", encoding="utf-8")
            omitted.write_text("validated\n", encoding="utf-8")
            run_git(repository, "add", "safe.txt", "omitted.txt")

            index = repository / ".git" / "index"
            full_index = root / "full-index"
            reduced_index = root / "reduced-index"
            shutil.copy2(index, full_index)
            run_git(repository, "rm", "--cached", "omitted.txt")
            shutil.copy2(index, reduced_index)
            shutil.copy2(full_index, index)

            build = repository / "build" / "generated"
            receipt = repository / "build" / "validation-receipt.json"
            write_complete_tree(build)
            real_run = subprocess.run
            substitution_count = 0

            def substitute_index_during_inventory(*args, **kwargs):
                nonlocal substitution_count
                command = args[0]
                if command[:2] == ["git", "ls-files"]:
                    displaced = repository / ".git" / "index.displaced"
                    index.rename(displaced)
                    shutil.copy2(reduced_index, index)
                    substitution_count += 1
                    try:
                        return real_run(*args, **kwargs)
                    finally:
                        index.unlink()
                        displaced.rename(index)
                return real_run(*args, **kwargs)

            real_source_digest = release_state.source_digest
            with patch.object(
                release_state.subprocess,
                "run",
                side_effect=substitute_index_during_inventory,
            ), patch.object(
                release_state,
                "source_digest",
                side_effect=lambda: real_source_digest(repository),
            ):
                try:
                    release_state.write_validation_receipt(build, receipt)
                except ValueError as error:
                    self.assertIn(
                        "Git metadata changed during source hashing",
                        str(error),
                    )
                    self.assertEqual(1, substitution_count)
                    self.assertFalse(receipt.exists())
                else:
                    omitted.write_text(
                        "changed after validation\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "source state"):
                        release_state.verify_validation_receipt(build, receipt)
                    self.assertEqual(2, substitution_count)
                    self.assertTrue(receipt.is_file())

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

    def test_tree_digest_binds_empty_directory_membership(self) -> None:
        with TemporaryDirectory(prefix="release-tree-directories-") as temp_root:
            root = Path(temp_root)
            write_complete_tree(root)
            before = release_state.tree_digest(root)
            (root / "stata-core" / "empty").mkdir()
            after = release_state.tree_digest(root)

        self.assertNotEqual(before, after)

    def test_tree_digest_type_tags_file_and_directory(self) -> None:
        file_digest = release_state.tree_digest_records(
            [("same-path", b"file", b"")]
        )
        directory_digest = release_state.tree_digest_records(
            [("same-path", b"directory", b"")]
        )

        self.assertNotEqual(file_digest, directory_digest)

    def test_tree_digest_rejects_noncanonical_file_mode(self) -> None:
        with TemporaryDirectory(prefix="release-tree-file-mode-") as temp_root:
            root = Path(temp_root)
            write_complete_tree(root)
            skill_file = root / "stata-core" / "SKILL.md"
            skill_file.chmod(0o666)

            with self.assertRaisesRegex(
                ValueError,
                "Tree file has noncanonical permissions 0666.*expected 0644",
            ):
                release_state.tree_digest(root)

    def test_tree_digest_rejects_noncanonical_directory_mode(self) -> None:
        with TemporaryDirectory(prefix="release-tree-directory-mode-") as temp_root:
            root = Path(temp_root)
            write_complete_tree(root)
            agents = root / "stata-core" / "agents"
            agents.chmod(0o777)

            with self.assertRaisesRegex(
                ValueError,
                "Tree directory has noncanonical permissions 0777.*expected 0755",
            ):
                release_state.tree_digest(root)

    def test_tree_digest_rejects_membership_change_during_walk(self) -> None:
        with TemporaryDirectory(prefix="release-tree-race-") as temp_root:
            root = Path(temp_root)
            write_complete_tree(root)
            real_listdir = release_state.os.listdir
            calls = 0

            def add_directory_after_root_listing(path):
                nonlocal calls
                names = real_listdir(path)
                calls += 1
                if calls == 1:
                    (root / "concurrent-empty").mkdir()
                return names

            with patch.object(
                release_state.os,
                "listdir",
                side_effect=add_directory_after_root_listing,
            ), self.assertRaisesRegex(ValueError, "changed while hashing"):
                release_state.tree_digest(root)

    def test_receipt_rejects_added_empty_directory(self) -> None:
        with TemporaryDirectory(prefix="release-tree-directories-") as temp_root:
            root = Path(temp_root)
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            with patch.object(release_state, "source_digest", return_value="source"):
                release_state.write_validation_receipt(build, receipt)
                (build / "stata-core" / "empty").mkdir()
                with self.assertRaisesRegex(ValueError, "build/generated"):
                    release_state.verify_validation_receipt(build, receipt)

    def test_receipt_rejects_permission_drift(self) -> None:
        with TemporaryDirectory(prefix="release-tree-mode-receipt-") as temp_root:
            root = Path(temp_root)
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            with patch.object(release_state, "source_digest", return_value="source"):
                release_state.write_validation_receipt(build, receipt)
                (build / "stata-core" / "SKILL.md").chmod(0o666)
                with self.assertRaisesRegex(
                    ValueError,
                    "noncanonical permissions 0666",
                ):
                    release_state.verify_validation_receipt(build, receipt)

    def test_schema_one_receipt_requires_revalidation(self) -> None:
        with TemporaryDirectory(prefix="release-schema-") as temp_root:
            receipt = Path(temp_root) / "receipt.json"
            receipt.write_text(
                json.dumps({"schema_version": 1}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "does not bind directory membership.*Run make validate",
            ):
                release_state.read_validation_receipt(receipt)

    def test_schema_two_receipt_requires_permission_revalidation(self) -> None:
        with TemporaryDirectory(prefix="release-schema-") as temp_root:
            receipt = Path(temp_root) / "receipt.json"
            receipt.write_text(
                json.dumps({"schema_version": 2}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "does not enforce canonical file and directory permissions"
                ".*Run make validate",
            ):
                release_state.read_validation_receipt(receipt)


if __name__ == "__main__":
    unittest.main()
