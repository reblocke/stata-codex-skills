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

    def test_combined_inventory_rejects_index_change_between_queries(self) -> None:
        with TemporaryDirectory(prefix="release-combined-index-race-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            tracked = root / "tracked.txt"
            tracked.write_text("tracked\n", encoding="utf-8")
            run_git(root, "add", "tracked.txt")
            late = root / "scripts" / "late.py"
            late.parent.mkdir()
            late.write_text("raise SystemExit(0)\n", encoding="utf-8")
            real_tracked_inventory = release_state._tracked_inventory
            calls = 0

            def stage_after_tracked_query(binding):
                nonlocal calls
                payload = real_tracked_inventory(binding)
                calls += 1
                if calls == 1:
                    run_git(root, "add", "scripts/late.py")
                return payload

            with patch.object(
                release_state,
                "_tracked_inventory",
                side_effect=stage_after_tracked_query,
            ), self.assertRaisesRegex(
                ValueError,
                "Git metadata changed during source hashing",
            ):
                release_state.source_path_inventory(root)

        self.assertEqual(1, calls)

    def test_untracked_content_blocks_receipt_write(self) -> None:
        with TemporaryDirectory(prefix="release-untracked-content-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            run_git(root, "add", ".gitignore")
            build = root / "build" / "generated"
            receipt = root / "build" / "validation-receipt.json"
            write_complete_tree(build)
            untracked = root / "content" / "core" / "new-command.yaml"
            untracked.parent.mkdir(parents=True)
            untracked.write_text("slug: new-command\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "Untracked, nonignored.*content/core/new-command.yaml",
            ):
                release_state.write_validation_receipt(
                    build,
                    receipt,
                    repo_root=root,
                )

            self.assertFalse(receipt.exists())

    def test_untracked_file_added_before_receipt_swap_blocks_write(self) -> None:
        with TemporaryDirectory(prefix="release-untracked-write-race-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            run_git(root, "add", ".gitignore")
            build = root / "build" / "generated"
            receipt = root / "build" / "validation-receipt.json"
            write_complete_tree(build)
            real_mkstemp = release_state.tempfile.mkstemp

            def add_input_after_temporary_receipt(*args, **kwargs):
                result = real_mkstemp(*args, **kwargs)
                source = root / "scripts" / "late_validator.py"
                source.parent.mkdir()
                source.write_text(
                    "raise SystemExit(0)\n",
                    encoding="utf-8",
                )
                return result

            with patch.object(
                release_state.tempfile,
                "mkstemp",
                side_effect=add_input_after_temporary_receipt,
            ), self.assertRaisesRegex(
                ValueError,
                "Untracked, nonignored.*scripts/late_validator.py",
            ):
                release_state.write_validation_receipt(
                    build,
                    receipt,
                    repo_root=root,
                )

            self.assertFalse(receipt.exists())
            self.assertEqual([], list(receipt.parent.glob(".*.tmp")))

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

    def test_ignored_runtime_files_are_allowed_for_receipts(self) -> None:
        with TemporaryDirectory(prefix="release-ignored-runtime-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            (root / ".gitignore").write_text(
                "build/\nraw/\n.cache/\n.venv/\n__pycache__/\ntests/tmp/\n",
                encoding="utf-8",
            )
            run_git(root, "add", ".gitignore")
            runtime_paths = (
                Path("raw/runtime.log"),
                Path(".cache/state.json"),
                Path(".venv/pyvenv.cfg"),
                Path("scripts/__pycache__/module.pyc"),
                Path("tests/tmp/output.txt"),
            )
            for relative in runtime_paths:
                runtime = root / relative
                runtime.parent.mkdir(parents=True, exist_ok=True)
                runtime.write_text("ignored diagnostics\n", encoding="utf-8")
            build = root / "build" / "generated"
            receipt = root / "build" / "validation-receipt.json"
            write_complete_tree(build)

            inventory = release_state.source_path_inventory(root)
            self.assertEqual((), inventory.untracked)
            self.assertEqual((), inventory.untracked_gate_inputs)
            release_state.write_validation_receipt(
                build,
                receipt,
                repo_root=root,
            )
            release_state.verify_validation_receipt(
                build,
                receipt,
                repo_root=root,
            )

            self.assertTrue(receipt.is_file())

    def test_receipt_recomputes_complete_state_before_atomic_replace(self) -> None:
        with TemporaryDirectory(prefix="release-final-state-") as temp_root:
            root = Path(temp_root)
            receipt = root / "validation-receipt.json"
            before = {
                "source_sha256": "source-before",
                "tree_sha256": "tree-before",
            }
            after = {
                "source_sha256": "source-after",
                "tree_sha256": "tree-before",
            }

            with patch.object(
                release_state,
                "validation_state",
                side_effect=(before, after),
            ) as validation, self.assertRaisesRegex(
                ValueError,
                "changed before receipt publication",
            ):
                release_state.write_validation_receipt(
                    root / "generated",
                    receipt,
                )

            self.assertEqual(2, validation.call_count)
            self.assertFalse(receipt.exists())
            self.assertEqual([], list(root.glob(".*.tmp")))

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

    def test_untracked_membership_change_during_inventory_fails(self) -> None:
        with TemporaryDirectory(prefix="release-untracked-race-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            tracked = root / "tracked.txt"
            tracked.write_text("tracked\n", encoding="utf-8")
            run_git(root, "add", "tracked.txt")
            real_inventory = release_state._untracked_inventory
            calls = 0

            def add_file_after_first_inventory(binding, repository_fd):
                nonlocal calls
                inventory = real_inventory(binding, repository_fd)
                calls += 1
                if calls == 1:
                    (root / "late.py").write_text(
                        "raise SystemExit(0)\n",
                        encoding="utf-8",
                    )
                return inventory

            with patch.object(
                release_state,
                "_untracked_inventory",
                side_effect=add_file_after_first_inventory,
            ), self.assertRaisesRegex(
                ValueError,
                "Untracked source membership changed during inventory",
            ):
                release_state._assert_no_untracked_source_files(root)

        self.assertEqual(2, calls)

    def test_index_change_during_untracked_inventory_fails(self) -> None:
        with TemporaryDirectory(prefix="release-untracked-index-race-") as temp_root:
            root = Path(temp_root)
            initialize_repository(root)
            tracked = root / "tracked.txt"
            tracked.write_text("tracked\n", encoding="utf-8")
            run_git(root, "add", "tracked.txt")
            late = root / "late.txt"
            late.write_text("late\n", encoding="utf-8")
            real_inventory = release_state._untracked_inventory
            calls = 0

            def change_index_after_first_inventory(binding, repository_fd):
                nonlocal calls
                inventory = real_inventory(binding, repository_fd)
                calls += 1
                if calls == 1:
                    run_git(root, "add", "late.txt")
                return inventory

            with patch.object(
                release_state,
                "_untracked_inventory",
                side_effect=change_index_after_first_inventory,
            ), self.assertRaisesRegex(
                ValueError,
                "Git metadata changed during source hashing",
            ):
                release_state._assert_no_untracked_source_files(root)

        self.assertEqual(1, calls)

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
                "validation_state",
                return_value={
                    "source_sha256": "after",
                    "tree_sha256": expected["tree_sha256"],
                },
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
