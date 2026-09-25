from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
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
    def test_source_digest_ignores_ambient_git_configuration(self) -> None:
        with TemporaryDirectory(prefix="release-git-environment-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            source = root / "sample.txt"
            source.write_text("tracked\n", encoding="utf-8")
            run_git(root, "add", "sample.txt")
            expected = release_state.source_digest(root)
            trace = root.parent / "foreign-git-trace"
            injected = {
                "GIT_CONFIG": "/tmp/foreign-config",
                "GIT_CONFIG_COUNT": "not-a-number",
                "GIT_CONFIG_GLOBAL": "/tmp/foreign-global-config",
                "GIT_CONFIG_KEY_0": "include.path",
                "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/foreign-hooks'",
                "GIT_CONFIG_SYSTEM": "/tmp/foreign-system-config",
                "GIT_CONFIG_VALUE_0": "/tmp/foreign-include",
                "GIT_EXEC_PATH": "/tmp/foreign-git-exec-path",
                "GIT_TRACE": str(trace),
                "GIT_TRACE2_EVENT": str(trace),
            }
            with patch.dict(release_state.os.environ, injected, clear=False):
                observed = release_state.source_digest(root)

        self.assertEqual(expected, observed)
        self.assertFalse(trace.exists())

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
                if (
                    command[:2] == ["git", "ls-files"]
                    and "--stage" in command
                ):
                    substitution_count += 1
                if substitution_count == 1 and "--stage" in command:
                    displaced = repository / ".git" / "index.displaced"
                    index.rename(displaced)
                    shutil.copy2(reduced_index, index)
                    try:
                        return real_run(*args, **kwargs)
                    finally:
                        index.unlink()
                        displaced.rename(index)
                return real_run(*args, **kwargs)

            with patch.object(
                release_state.subprocess,
                "run",
                side_effect=substitute_index_during_inventory,
            ):
                try:
                    release_state.write_validation_receipt(
                        build,
                        receipt,
                        repo_root=repository,
                    )
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
                        release_state.verify_validation_receipt(
                            build,
                            receipt,
                            repo_root=repository,
                        )
                    self.assertGreaterEqual(substitution_count, 2)
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

            release_state.write_validation_receipt(
                build,
                receipt,
                repo_root=root,
            )
            tracked.write_text("second\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source state"):
                release_state.verify_validation_receipt(
                    build,
                    receipt,
                    repo_root=root,
                )

    def test_untracked_inventory_uses_repository_owned_ignores_only(self) -> None:
        with TemporaryDirectory(prefix="release-untracked-paths-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text("raw/\n", encoding="utf-8")
            run_git(root, "add", ".gitignore")
            ignored = root / "raw" / "runtime.log"
            ignored.parent.mkdir()
            ignored.write_text("ignored runtime\n", encoding="utf-8")
            local_exclude = root / ".git" / "info" / "exclude"
            local_exclude.write_text("locally-hidden.py\n", encoding="utf-8")
            (root / "locally-hidden.py").write_text(
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            source = root / "scripts" / "new_validator.py"
            source.parent.mkdir()
            source.write_text("raise SystemExit(0)\n", encoding="utf-8")

            observed = release_state.untracked_source_paths(root)

        self.assertEqual(
            (
                Path("locally-hidden.py"),
                Path("scripts/new_validator.py"),
            ),
            observed,
        )

    def test_ignored_untracked_gate_inputs_are_detected(self) -> None:
        with TemporaryDirectory(prefix="release-ignored-gate-inputs-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            ignored_inputs = (
                Path(".github/workflows/extra.yml"),
                Path("config/extra.yaml"),
                Path("content/core/extra.yaml"),
                Path("locks/extra.yaml"),
                Path("manifests/extra.yaml"),
                Path("scripts/extra.py"),
                Path("templates/extra.md.j2"),
                Path("tests/test_extra.py"),
                Path("Makefile"),
                Path("pyproject.toml"),
                Path("uv.lock"),
            )
            (root / ".gitignore").write_text(
                "\n".join(f"/{path.as_posix()}" for path in ignored_inputs)
                + "\n",
                encoding="utf-8",
            )
            run_git(root, "add", ".gitignore")
            for relative in ignored_inputs:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("ignored gate input\n", encoding="utf-8")

            inventory = release_state.source_path_inventory(root)

            self.assertEqual((), inventory.untracked)
            self.assertEqual(
                tuple(sorted(ignored_inputs, key=Path.as_posix)),
                inventory.untracked_gate_inputs,
            )
            with self.assertRaisesRegex(
                ValueError,
                "Ignored, untracked validation inputs.*scripts/extra.py",
            ):
                release_state._assert_no_untracked_source_files(
                    root,
                    inventory=inventory,
                )

    def test_nested_runtime_named_content_is_still_a_gate_input(self) -> None:
        with TemporaryDirectory(prefix="release-nested-runtime-name-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text(
                "content/build/\nbuild/\n",
                encoding="utf-8",
            )
            run_git(root, "add", ".gitignore")
            hidden = root / "content" / "build" / "hidden.yaml"
            hidden.parent.mkdir(parents=True)
            hidden.write_text("slug: hidden\n", encoding="utf-8")

            inventory = release_state.source_path_inventory(root)

            self.assertEqual((), inventory.untracked)
            self.assertIn(
                Path("content/build/hidden.yaml"),
                inventory.untracked_gate_inputs,
            )
            with self.assertRaisesRegex(
                ValueError,
                "Ignored, untracked validation inputs.*content/build/hidden.yaml",
            ):
                release_state._assert_no_untracked_source_files(
                    root,
                    inventory=inventory,
                )

    def test_untracked_file_added_before_receipt_swap_blocks_write(self) -> None:
        with TemporaryDirectory(prefix="release-untracked-write-race-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            run_git(root, "add", ".gitignore")
            build = root / "build" / "generated"
            receipt = root / "build" / "validation-receipt.json"
            write_complete_tree(build)
            real_create_temporary = (
                release_state._create_receipt_temporary
            )

            def add_input_after_temporary_receipt(
                transaction,
                payload,
            ):
                result = real_create_temporary(transaction, payload)
                source = root / "scripts" / "late_validator.py"
                source.parent.mkdir()
                source.write_text(
                    "raise SystemExit(0)\n",
                    encoding="utf-8",
                )
                return result

            with patch.object(
                release_state,
                "_create_receipt_temporary",
                side_effect=add_input_after_temporary_receipt,
            ), self.assertRaisesRegex(
                ValueError,
                "Untracked, nonignored.*scripts/late_validator.py",
            ) as raised:
                release_state.write_validation_receipt(
                    build,
                    receipt,
                    repo_root=root,
                )

            self.assertFalse(receipt.exists())
            retained = list(
                receipt.parent.glob(
                    f".{receipt.name}.tmp-*"
                )
            )
            self.assertEqual(1, len(retained))
            self.assertIn(
                "retained validation receipt state",
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )

    def test_untracked_python_blocks_receipt_verification(self) -> None:
        with TemporaryDirectory(prefix="release-untracked-python-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            run_git(root, "add", ".gitignore")
            build = root / "build" / "generated"
            receipt = root / "build" / "validation-receipt.json"
            write_complete_tree(build)
            release_state.write_validation_receipt(
                build,
                receipt,
                repo_root=root,
            )
            untracked = root / "scripts" / "new_validator.py"
            untracked.parent.mkdir()
            untracked.write_text("raise SystemExit(0)\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "Untracked, nonignored.*scripts/new_validator.py",
            ):
                release_state.verify_validation_receipt(
                    build,
                    receipt,
                    repo_root=root,
                )

    def test_late_public_receipt_is_not_replaced(self) -> None:
        with TemporaryDirectory(prefix="release-receipt-late-") as temp_root:
            root = Path(temp_root)
            receipt = root / "validation-receipt.json"
            state = {
                "source_sha256": "stable-source",
                "tree_sha256": "stable-tree",
            }
            real_rename = release_state.atomic_rename_at_no_replace
            appeared = False

            def create_public_name_before_placement(
                source_descriptor,
                source_name,
                destination_descriptor,
                destination_name,
            ):
                nonlocal appeared
                if (
                    destination_name == receipt.name
                    and source_name.startswith(f".{receipt.name}.tmp-")
                ):
                    appeared = True
                    receipt.write_text(
                        "late receipt bytes\n",
                        encoding="utf-8",
                    )
                return real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.object(
                release_state,
                "validation_state",
                return_value=state,
            ), patch.object(
                release_state,
                "atomic_rename_at_no_replace",
                side_effect=create_public_name_before_placement,
            ), self.assertRaises(FileExistsError) as raised:
                release_state.write_validation_receipt(
                    root / "generated",
                    receipt,
                )

            self.assertTrue(appeared)
            self.assertEqual(
                "late receipt bytes\n",
                receipt.read_text(encoding="utf-8"),
            )
            retained = list(root.glob(f".{receipt.name}.tmp-*"))
            self.assertEqual(1, len(retained))
            self.assertIn(
                str(retained[0]),
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )

    def test_parent_move_before_publication_preserves_replacement(self) -> None:
        with TemporaryDirectory(prefix="release-receipt-parent-") as temp_root:
            outer = Path(temp_root)
            accepted_parent = outer / "accepted"
            accepted_parent.mkdir()
            moved_parent = outer / "accepted-moved"
            receipt = accepted_parent / "validation-receipt.json"
            state = {
                "source_sha256": "stable-source",
                "tree_sha256": "stable-tree",
            }
            real_publish = release_state._publish_receipt_temporary
            replacement_marker = accepted_parent / "valuable.txt"

            def move_parent_before_publication(transaction):
                accepted_parent.rename(moved_parent)
                accepted_parent.mkdir()
                replacement_marker.write_text(
                    "preserve replacement\n",
                    encoding="utf-8",
                )
                return real_publish(transaction)

            with patch.object(
                release_state,
                "validation_state",
                return_value=state,
            ), patch.object(
                release_state,
                "_publish_receipt_temporary",
                side_effect=move_parent_before_publication,
            ), self.assertRaisesRegex(
                release_state.ReceiptTransactionError,
                "receipt parent changed",
            ) as raised:
                release_state.write_validation_receipt(
                    outer / "generated",
                    receipt,
                )

            self.assertEqual(
                "preserve replacement\n",
                replacement_marker.read_text(encoding="utf-8"),
            )
            self.assertFalse(receipt.exists())
            retained = list(
                moved_parent.glob(f".{receipt.name}.tmp-*")
            )
            self.assertEqual(1, len(retained))
            self.assertIn(
                str(retained[0]),
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )

    def test_same_inode_temporary_byte_change_is_preserved_and_rejected(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="release-receipt-bytes-") as temp_root:
            root = Path(temp_root)
            receipt = root / "validation-receipt.json"
            state = {
                "source_sha256": "stable-source",
                "tree_sha256": "stable-tree",
            }
            real_publish = release_state._publish_receipt_temporary
            changed_temporary: Path | None = None
            before_fds = len(os.listdir("/dev/fd"))

            def change_temporary_bytes(transaction):
                nonlocal changed_temporary
                assert transaction.temporary_name is not None
                changed_temporary = root / transaction.temporary_name
                before = changed_temporary.stat()
                changed_temporary.write_bytes(b"changed temporary bytes\n")
                after = changed_temporary.stat()
                self.assertEqual(
                    (before.st_dev, before.st_ino),
                    (after.st_dev, after.st_ino),
                )
                return real_publish(transaction)

            with patch.object(
                release_state,
                "validation_state",
                return_value=state,
            ), patch.object(
                release_state,
                "_publish_receipt_temporary",
                side_effect=change_temporary_bytes,
            ), self.assertRaisesRegex(
                release_state.ReceiptTransactionError,
                "temporary changed before publication",
            ) as raised:
                release_state.write_validation_receipt(
                    root / "generated",
                    receipt,
                )

            self.assertIsNotNone(changed_temporary)
            assert changed_temporary is not None
            self.assertFalse(receipt.exists())
            self.assertEqual(
                b"changed temporary bytes\n",
                changed_temporary.read_bytes(),
            )
            self.assertIn(
                str(changed_temporary),
                "\n".join(getattr(raised.exception, "__notes__", ())),
            )
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))

    def test_untracked_inventory_ignores_worktree_fsmonitor_config(self) -> None:
        with TemporaryDirectory(prefix="release-fsmonitor-") as temp_root:
            outer = Path(temp_root)
            root = outer / "repository"
            root.mkdir()
            initialize_repository(root)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            run_git(root, "add", ".gitignore")
            marker = outer / "fsmonitor-invoked"
            monitor = outer / "malicious-fsmonitor"
            monitor.write_text(
                "#!/bin/sh\n"
                f"touch '{marker}'\n"
                "exit 0\n",
                encoding="utf-8",
            )
            monitor.chmod(0o755)
            run_git(root, "config", "extensions.worktreeConfig", "true")
            run_git(root, "config", "--worktree", "core.fsmonitor", str(monitor))
            build = root / "build" / "generated"
            receipt = root / "build" / "validation-receipt.json"
            write_complete_tree(build)
            real_run = release_state.subprocess.run
            untracked_calls = 0

            def inspect_private_inventory(*args, **kwargs):
                nonlocal untracked_calls
                command = args[0]
                if command[:2] == ["git", "ls-files"] and "--others" in command:
                    untracked_calls += 1
                    environment = kwargs["env"]
                    self.assertEqual(os.devnull, environment["GIT_CONFIG_GLOBAL"])
                    self.assertEqual("1", environment["GIT_CONFIG_NOSYSTEM"])
                    private_config = (
                        Path(environment["GIT_DIR"]) / "config"
                    ).read_text(encoding="utf-8")
                    self.assertIn("\tfsmonitor = false\n", private_config)
                    self.assertIn(
                        f"\thooksPath = {os.devnull}\n",
                        private_config,
                    )
                return real_run(*args, **kwargs)

            with patch.object(
                release_state.subprocess,
                "run",
                side_effect=inspect_private_inventory,
            ):
                self.assertEqual(
                    (),
                    release_state.untracked_source_paths(root),
                )
                release_state.write_validation_receipt(
                    build,
                    receipt,
                    repo_root=root,
                )

            self.assertGreaterEqual(untracked_calls, 2)
            self.assertFalse(marker.exists())

    def test_receipt_rejects_tree_or_source_drift(self) -> None:
        with TemporaryDirectory(prefix="release-receipt-") as temp_root:
            root = Path(temp_root)
            repository = root / "repository"
            repository.mkdir()
            initialize_repository(repository)
            source = repository / "source.txt"
            source.write_text("source-a\n", encoding="utf-8")
            run_git(repository, "add", "source.txt")
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            release_state.write_validation_receipt(
                build,
                receipt,
                repo_root=repository,
            )
            release_state.verify_validation_receipt(
                build,
                receipt,
                repo_root=repository,
            )

            (build / "stata-core" / "SKILL.md").write_text(
                "# changed\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "build/generated"):
                release_state.verify_validation_receipt(
                    build,
                    receipt,
                    repo_root=repository,
                )

            write_complete_tree(build)
            source.write_text("source-b\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source state"):
                release_state.verify_validation_receipt(
                    build,
                    receipt,
                    repo_root=repository,
                )

    def test_receipt_rejects_added_empty_directory(self) -> None:
        with TemporaryDirectory(prefix="release-tree-directories-") as temp_root:
            root = Path(temp_root)
            repository = root / "repository"
            repository.mkdir()
            initialize_repository(repository)
            source = repository / "source.txt"
            source.write_text("source\n", encoding="utf-8")
            run_git(repository, "add", "source.txt")
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            release_state.write_validation_receipt(
                build,
                receipt,
                repo_root=repository,
            )
            (build / "stata-core" / "empty").mkdir()
            with self.assertRaisesRegex(ValueError, "build/generated"):
                release_state.verify_validation_receipt(
                    build,
                    receipt,
                    repo_root=repository,
                )

    def test_receipt_rejects_permission_drift(self) -> None:
        with TemporaryDirectory(prefix="release-tree-mode-receipt-") as temp_root:
            root = Path(temp_root)
            repository = root / "repository"
            repository.mkdir()
            initialize_repository(repository)
            source = repository / "source.txt"
            source.write_text("source\n", encoding="utf-8")
            run_git(repository, "add", "source.txt")
            build = root / "generated"
            receipt = root / "receipt.json"
            write_complete_tree(build)
            release_state.write_validation_receipt(
                build,
                receipt,
                repo_root=repository,
            )
            (build / "stata-core" / "SKILL.md").chmod(0o666)
            with self.assertRaisesRegex(
                ValueError,
                "noncanonical permissions 0666",
            ):
                release_state.verify_validation_receipt(
                    build,
                    receipt,
                    repo_root=repository,
                )

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
