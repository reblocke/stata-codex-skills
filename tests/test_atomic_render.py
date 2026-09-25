from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_determinism  # noqa: E402
import lint_skill_pack  # noqa: E402
import render_skills  # noqa: E402


def repository_test_tmp_root() -> Path:
    """Return an ignored scratch root on the repository filesystem."""

    root = REPO_ROOT / "tests" / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"unsafe repository test scratch root: {root}")
    if root.stat().st_dev != REPO_ROOT.stat().st_dev:
        raise RuntimeError(
            f"repository test scratch root is on another filesystem: {root}"
        )
    return root


class AtomicRenderTests(unittest.TestCase):
    @staticmethod
    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    @staticmethod
    def transaction_artifacts(parent: Path, target_name: str) -> list[Path]:
        prefixes = (
            f".{target_name}.stage-",
            f".{target_name}.backup-",
            f".{target_name}.recovery-",
        )
        return [
            path
            for path in parent.iterdir()
            if path.name.startswith(prefixes)
        ]

    def assert_single_retained_stage(
        self,
        parent: Path,
        target_name: str,
    ) -> Path:
        artifacts = self.transaction_artifacts(parent, target_name)
        self.assertEqual(1, len(artifacts))
        self.assertIn(".stage-", artifacts[0].name)
        self.assertTrue(artifacts[0].is_dir())
        return artifacts[0]

    @staticmethod
    def seeded_target(parent: Path) -> Path:
        target = parent / "generated"
        render_skills.render_all(output_root=target)
        (target / "stata-core" / "SKILL.md").write_text(
            "# Prior generated tree\n",
            encoding="utf-8",
        )
        return target

    def test_success_replaces_complete_tree_without_transaction_artifacts(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)

            output = io.StringIO()
            with redirect_stdout(output):
                render_skills.render_all(output_root=target)

            self.assertNotEqual(
                b"# Prior generated tree\n",
                (target / "stata-core" / "SKILL.md").read_bytes(),
            )
            self.assertEqual(
                {"stata-core", "stata-packages", "stata-c-plugins"},
                {path.name for path in target.iterdir() if path.is_dir()},
            )
            self.assertEqual(
                [],
                self.transaction_artifacts(parent, target.name),
            )
            self.assertNotIn(
                "retained for explicit cleanup",
                output.getvalue().lower(),
            )

    def test_render_normalizes_modes_even_with_permissive_umask(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-modes-") as temp_root:
            target = Path(temp_root) / "generated"
            prior_umask = os.umask(0)
            try:
                render_skills.render_all(output_root=target)
            finally:
                os.umask(prior_umask)

            for path in [target, *sorted(target.rglob("*"))]:
                metadata = path.lstat()
                expected_mode = 0o755 if path.is_dir() else 0o644
                self.assertEqual(
                    expected_mode,
                    metadata.st_mode & 0o7777,
                    path,
                )

    def test_changed_output_parent_before_staging_cannot_redirect_render(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            root = Path(temp_root)
            approved_parent = root / "approved-parent"
            approved_parent.mkdir()
            target = self.seeded_target(approved_parent)
            prior = self.snapshot(target)
            displaced_parent = root / "displaced-approved-parent"
            replacement_sentinel = approved_parent / "valuable.txt"
            real_create_stage = render_skills._create_staged_root_at
            changed = False

            def change_parent_after_stage_creation(
                parent_handle: render_skills.RenderParentHandle,
                output_name: str,
            ) -> tuple[Path, os.stat_result]:
                nonlocal changed
                created = real_create_stage(parent_handle, output_name)
                parent_handle.path.rename(displaced_parent)
                parent_handle.path.mkdir()
                replacement_sentinel.write_text(
                    "preserve replacement parent\n",
                    encoding="utf-8",
                )
                changed = True
                return created

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_create_staged_root_at",
                side_effect=change_parent_after_stage_creation,
            ), redirect_stdout(output), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "output parent changed after validation",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(changed)
            self.assertEqual(
                "preserve replacement parent\n",
                replacement_sentinel.read_text(encoding="utf-8"),
            )
            self.assertFalse((approved_parent / target.name).exists())
            self.assertEqual(
                prior,
                self.snapshot(displaced_parent / target.name),
            )
            stages = [
                path
                for path in displaced_parent.iterdir()
                if path.name.startswith(f".{target.name}.stage-")
            ]
            self.assertEqual(1, len(stages))
            self.assertIn(
                "no trusted entry manifest was captured",
                output.getvalue(),
            )

    def test_stage_substitution_before_first_write_is_not_modified(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            displaced_stage = parent / "created-stage"
            replacement_sentinel: Path | None = None
            real_write = render_skills._write_staged_text
            changed = False

            def substitute_stage_before_write(
                parent_handle: render_skills.RenderParentHandle,
                staged_root: Path,
                staged_identity: tuple[int, int],
                directory_identities: dict[
                    tuple[str, ...],
                    tuple[int, int],
                ],
                relative_path: Path,
                text: str,
            ) -> None:
                nonlocal changed, replacement_sentinel
                if not changed:
                    staged_root.rename(displaced_stage)
                    staged_root.mkdir()
                    replacement_sentinel = staged_root / "valuable.txt"
                    replacement_sentinel.write_text(
                        "preserve replacement stage\n",
                        encoding="utf-8",
                    )
                    changed = True
                real_write(
                    parent_handle,
                    staged_root,
                    staged_identity,
                    directory_identities,
                    relative_path,
                    text,
                )

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_write_staged_text",
                side_effect=substitute_stage_before_write,
            ), redirect_stdout(output), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "staging root changed before writing",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(changed)
            self.assertEqual(prior, self.snapshot(target))
            self.assertIsNotNone(replacement_sentinel)
            assert replacement_sentinel is not None
            self.assertEqual(
                "preserve replacement stage\n",
                replacement_sentinel.read_text(encoding="utf-8"),
            )
            self.assertTrue(displaced_stage.is_dir())
            self.assertIn(
                str(displaced_stage.resolve()),
                output.getvalue(),
            )

    def test_render_failure_preserves_previous_tree(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)

            def fail_after_partial_render(
                output_root: Path,
                *_args: object,
            ) -> None:
                (output_root / "partial").mkdir()
                raise RuntimeError("forced render failure")

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_render_tree",
                side_effect=fail_after_partial_render,
            ), redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, "forced render failure"):
                    render_skills.render_all(output_root=target)

            self.assertEqual(before, self.snapshot(target))
            stages = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".stage-" in path.name
            ]
            self.assertEqual(1, len(stages))
            self.assertTrue((stages[0] / "partial").is_dir())
            self.assertIn(
                "no trusted entry manifest was captured",
                output.getvalue(),
            )

    def test_substituted_stage_path_is_not_recursively_deleted(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            displaced_stage = parent / "displaced-stage"
            unrelated = parent / "unrelated-stage"
            unrelated.mkdir()
            sentinel = unrelated / "valuable.txt"
            sentinel.write_text("preserve stage-path bytes\n", encoding="utf-8")

            def substitute_stage_then_fail(
                _parent_handle: render_skills.RenderParentHandle,
                staged_root: Path,
                _output_root: Path,
                _expected: render_skills.RenderOutputState,
                _expected_staged: render_skills.RenderOutputState,
                _validator: object,
                _verify_inputs: object,
            ) -> None:
                staged_root.rename(displaced_stage)
                unrelated.rename(staged_root)
                raise RuntimeError("forced failure after stage substitution")

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_replace_rendered_tree",
                side_effect=substitute_stage_then_fail,
            ), redirect_stdout(output), self.assertRaisesRegex(
                RuntimeError,
                "forced failure after stage substitution",
            ):
                render_skills.render_all(output_root=target)

            self.assertIn("staged render cleanup was skipped", output.getvalue())
            self.assertIn(
                str(displaced_stage.resolve()),
                output.getvalue(),
            )
            surviving_values = [
                path.read_text(encoding="utf-8")
                for path in parent.rglob("valuable.txt")
            ]
            self.assertEqual(["preserve stage-path bytes\n"], surviving_values)
            self.assertTrue(displaced_stage.is_dir())

    def test_stage_substitution_inside_install_is_quarantined_and_rolled_back(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            validated_stage = parent / "validated-stage"
            real_rename = render_skills._rename_render_entry
            rename_count = 0

            def substitute_during_install(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
            ) -> None:
                nonlocal rename_count
                rename_count += 1
                if rename_count == 2:
                    source = parent_handle.path / source_name
                    source.rename(validated_stage)
                    shutil.copytree(validated_stage, source)
                    (source / "stata-core" / "SKILL.md").write_text(
                        "# Unvalidated placement\n",
                        encoding="utf-8",
                    )
                real_rename(
                    parent_handle,
                    source_name,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=substitute_during_install,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "failed identity or content validation",
            ):
                render_skills.render_all(output_root=target)

            self.assertEqual(prior, self.snapshot(target))
            recoveries = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".recovery-" in path.name
            ]
            self.assertEqual(1, len(recoveries))
            self.assertEqual(
                "# Unvalidated placement\n",
                (
                    recoveries[0] / "stata-core" / "SKILL.md"
                ).read_text(encoding="utf-8"),
            )
            self.assertTrue(validated_stage.is_dir())

    def test_validation_failure_preserves_previous_tree(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)

            real_render = render_skills._render_tree

            def render_incomplete_tree(
                output_root: Path,
                *args: object,
            ) -> None:
                real_render(output_root, *args)
                (output_root / "stata-core" / "SKILL.md").unlink()

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_render_tree",
                side_effect=render_incomplete_tree,
            ), redirect_stdout(output):
                with self.assertRaisesRegex(
                    ValueError,
                    "staged render validation failed.*stata-core/SKILL.md",
                ):
                    render_skills.render_all(output_root=target)

            self.assertEqual(before, self.snapshot(target))
            stages = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".stage-" in path.name
            ]
            self.assertEqual(1, len(stages))
            self.assertTrue(stages[0].is_dir())
            self.assertIn(
                "staged render tree retained for explicit cleanup",
                output.getvalue(),
            )

    def test_stage_change_after_validation_is_not_trusted(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_validate = render_skills.validate_rendered_state
            validation_count = 0
            changed_stage: Path | None = None

            def validate_then_change_same_file(
                state: render_skills.RenderOutputState,
                *args: object,
            ) -> None:
                nonlocal validation_count, changed_stage
                real_validate(state, *args)
                validation_count += 1
                if validation_count == 1:
                    changed_stage = next(
                        path
                        for path in self.transaction_artifacts(
                            parent,
                            target.name,
                        )
                        if ".stage-" in path.name
                    )
                    skill_file = changed_stage / "stata-core" / "SKILL.md"
                    skill_file.write_text(
                        "# Changed after validation\n",
                        encoding="utf-8",
                    )

            output = io.StringIO()
            with patch.object(
                render_skills,
                "validate_rendered_state",
                side_effect=validate_then_change_same_file,
            ), redirect_stdout(output), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "private pre-delete verification|descriptor capture",
            ):
                render_skills.render_all(output_root=target)

            self.assertIsNotNone(changed_stage)
            self.assertEqual(prior, self.snapshot(target))
            self.assertEqual(
                "# Changed after validation\n",
                (
                    changed_stage / "stata-core" / "SKILL.md"
                ).read_text(encoding="utf-8"),
            )
            self.assertIn("staged render cleanup was skipped", output.getvalue())

    def test_truncated_skill_config_cannot_replace_complete_tree(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)
            config = yaml.safe_load(
                (REPO_ROOT / "config" / "skills.yaml").read_text(encoding="utf-8")
            )
            del config["skills"]["plugins"]
            config_path = parent / "truncated-skills.yaml"
            config_path.write_text(
                yaml.safe_dump(config, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "must define exactly core, packages, and plugins",
            ):
                render_skills.render_all(
                    output_root=target,
                    config_path=config_path,
                )

            self.assertEqual(before, self.snapshot(target))
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_failed_swap_rolls_back_previous_tree(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)
            real_replace = render_skills._rename_render_entry
            replace_count = 0

            def fail_new_tree_swap(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
            ) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count == 2:
                    raise OSError("forced swap failure")
                real_replace(
                    parent_handle,
                    source_name,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=fail_new_tree_swap,
            ):
                with self.assertRaisesRegex(OSError, "forced swap failure"):
                    render_skills.render_all(output_root=target)

            self.assertEqual(3, replace_count)
            self.assertEqual(before, self.snapshot(target))
            self.assert_single_retained_stage(parent, target.name)

    def test_keyboard_interrupt_after_backup_move_restores_previous_tree(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_verify = render_skills._verify_output_state_at
            interrupted = False

            def interrupt_after_backup_move(
                parent_handle: render_skills.RenderParentHandle,
                name: str,
                backup: Path,
                expected: render_skills.RenderOutputState,
            ) -> None:
                nonlocal interrupted
                if ".backup-" in name and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("forced interrupt after backup move")
                real_verify(parent_handle, name, backup, expected)

            with patch.object(
                render_skills,
                "_verify_output_state_at",
                side_effect=interrupt_after_backup_move,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "forced interrupt after backup move",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(interrupted)
            self.assertEqual(prior, self.snapshot(target))
            self.assert_single_retained_stage(parent, target.name)

    def test_failed_restore_preserves_prior_tree_at_reported_backup(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)
            real_replace = render_skills._rename_render_entry
            replace_count = 0

            def fail_swap_and_restore(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
            ) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count >= 2:
                    raise OSError(f"forced replace failure {replace_count}")
                real_replace(
                    parent_handle,
                    source_name,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=fail_swap_and_restore,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "retained prior-tree location",
            ):
                render_skills.render_all(output_root=target)

            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertFalse(target.exists())
            self.assertEqual(1, len(backups))
            self.assertEqual(before, self.snapshot(backups[0]))

    def test_backup_cleanup_failure_fails_after_successful_commit(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)

            def fail_backup_cleanup(
                path: Path,
                expected: render_skills.RenderOutputState,
                _parent_handle: render_skills.RenderParentHandle,
            ) -> None:
                raise PermissionError("forced backup cleanup failure")

            with patch.object(
                render_skills,
                "_remove_verified_backup",
                side_effect=fail_backup_cleanup,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "rendered tree was committed.*forced backup cleanup failure",
            ):
                render_skills.render_all(output_root=target)

            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(before, self.snapshot(backups[0]))
            self.assertNotEqual(before, self.snapshot(target))

    def test_post_removal_cleanup_error_reports_no_verified_survivor(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            real_remove = render_skills._remove_verified_backup

            def remove_then_fail(
                *args: object,
                **kwargs: object,
            ) -> None:
                real_remove(*args, **kwargs)
                raise OSError("forced post-removal durability failure")

            with patch.object(
                render_skills,
                "_remove_verified_backup",
                side_effect=remove_then_fail,
            ), self.assertRaises(
                render_skills.RenderTransactionError,
            ) as raised:
                render_skills.render_all(output_root=target)

            message = str(raised.exception)
            self.assertIn(
                "no verified surviving prior-tree path was found",
                message,
            )
            self.assertIn(
                "forced post-removal durability failure",
                message,
            )
            self.assertNotIn(
                "verified surviving prior-tree location",
                message,
            )
            self.assertEqual(
                [],
                self.transaction_artifacts(parent, target.name),
            )

    def test_backup_substitution_during_cleanup_is_not_deleted(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            accepted_backup = parent / "accepted-backup"
            concurrent = parent / "concurrent-backup"
            concurrent.mkdir()
            sentinel = concurrent / "valuable.txt"
            sentinel.write_text("preserve concurrent bytes\n", encoding="utf-8")
            prior = self.snapshot(target)
            real_remove = render_skills._remove_verified_backup
            substituted = False

            def substitute_before_cleanup(
                backup: Path,
                expected: render_skills.RenderOutputState,
                parent_handle: render_skills.RenderParentHandle,
            ) -> None:
                nonlocal substituted
                substituted = True
                backup.rename(accepted_backup)
                concurrent.rename(backup)
                real_remove(backup, expected, parent_handle)

            with patch.object(
                render_skills,
                "_remove_verified_backup",
                side_effect=substitute_before_cleanup,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "verified surviving prior-tree location",
            ) as raised:
                render_skills.render_all(output_root=target)

            self.assertTrue(substituted)
            self.assertIn(str(accepted_backup), str(raised.exception))
            self.assertEqual(prior, self.snapshot(accepted_backup))
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(
                "preserve concurrent bytes\n",
                (backups[0] / "valuable.txt").read_text(encoding="utf-8"),
            )

    def test_backup_cleanup_preserves_entry_added_through_open_descriptor(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_verify = render_skills._verify_directory_descriptor_tree
            injected = False
            verification_count = 0

            def add_entry_after_backup_verification(
                descriptor: int,
                display_path: Path,
                expected_entries: dict[str, render_skills.RenderTreeEntry],
                relative_parts: tuple[str, ...] = (),
            ) -> None:
                nonlocal injected, verification_count
                real_verify(
                    descriptor,
                    display_path,
                    expected_entries,
                    relative_parts,
                )
                if not relative_parts and ".backup-" in display_path.name:
                    verification_count += 1
                if (
                    not relative_parts
                    and ".backup-" in display_path.name
                    and verification_count == 1
                ):
                    injected = True
                    file_descriptor = os.open(
                        "late-added.txt",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=descriptor,
                    )
                    try:
                        os.write(file_descriptor, b"preserve late bytes\n")
                    finally:
                        os.close(file_descriptor)

            with patch.object(
                render_skills,
                "_verify_directory_descriptor_tree",
                side_effect=add_entry_after_backup_verification,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "verified cleanup of the accepted prior tree failed",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(injected)
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(
                "preserve late bytes\n",
                (backups[0] / "late-added.txt").read_text(encoding="utf-8"),
            )
            backup_snapshot = self.snapshot(backups[0])
            backup_snapshot.pop("late-added.txt")
            self.assertEqual(prior, backup_snapshot)

    def test_backup_cleanup_preserves_same_inode_content_mutation(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-content-race"
            backup.mkdir()
            accepted = backup / "owned.txt"
            accepted.write_text("accepted generated bytes\n", encoding="utf-8")
            metadata = backup.stat()
            expected_entries = render_skills._capture_tree_entries(backup)
            real_rename = render_skills._atomic_rename_at_no_replace
            mutated = False

            def append_after_capture_before_quarantine(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal mutated
                if source_name == "owned.txt" and not mutated:
                    mutated = True
                    file_descriptor = os.open(
                        source_name,
                        os.O_WRONLY | os.O_APPEND,
                        dir_fd=source_descriptor,
                    )
                    try:
                        os.write(
                            file_descriptor,
                            b"foreign appended bytes\n",
                        )
                    finally:
                        os.close(file_descriptor)
                real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_atomic_rename_at_no_replace",
                side_effect=append_after_capture_before_quarantine,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "file contents changed before deletion and were preserved",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertTrue(mutated)
            self.assertEqual(
                "accepted generated bytes\nforeign appended bytes\n",
                accepted.read_text(encoding="utf-8"),
            )

    def test_nonempty_backup_root_displacement_preserves_complete_tree(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-nonempty-root-race"
            accepted = parent / "accepted-backup-root"
            backup.mkdir()
            (backup / "alpha.txt").write_text(
                "accepted alpha bytes\n",
                encoding="utf-8",
            )
            nested = backup / "nested"
            nested.mkdir()
            (nested / "beta.txt").write_text(
                "accepted beta bytes\n",
                encoding="utf-8",
            )
            accepted_snapshot = self.snapshot(backup)
            metadata = backup.stat()
            expected_entries = render_skills._capture_tree_entries(backup)
            real_rename = render_skills._atomic_rename_at_no_replace
            displaced = False

            def displace_root_before_private_move(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal displaced
                if source_name == backup.name and not displaced:
                    os.rename(
                        source_name,
                        accepted.name,
                        src_dir_fd=source_descriptor,
                        dst_dir_fd=source_descriptor,
                    )
                    os.mkdir(
                        source_name,
                        0o700,
                        dir_fd=source_descriptor,
                    )
                    (backup / "foreign.txt").write_text(
                        "foreign replacement bytes\n",
                        encoding="utf-8",
                    )
                    displaced = True
                real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_atomic_rename_at_no_replace",
                side_effect=displace_root_before_private_move,
            ), patch.object(
                render_skills,
                "_clear_directory_descriptor",
                wraps=render_skills._clear_directory_descriptor,
            ) as clear_directory, self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "root changed while moving into private cleanup "
                "quarantine.*preserved",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertTrue(displaced)
            clear_directory.assert_not_called()
            self.assertEqual(accepted_snapshot, self.snapshot(accepted))
            self.assertTrue((accepted / "nested").is_dir())
            self.assertEqual(
                {"foreign.txt": b"foreign replacement bytes\n"},
                self.snapshot(backup),
            )
            self.assertEqual(
                [],
                [
                    path
                    for path in parent.iterdir()
                    if path.name.startswith(
                        render_skills.PRIVATE_CLEANUP_PREFIX
                    )
                ],
            )

    def test_install_no_replace_preserves_last_moment_output(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_rename = render_skills._rename_render_entry
            rename_count = 0

            def create_output_immediately_before_install(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
            ) -> None:
                nonlocal rename_count
                rename_count += 1
                if rename_count == 2:
                    target.mkdir()
                    (target / "valuable.txt").write_text(
                        "last-moment output\n",
                        encoding="utf-8",
                    )
                real_rename(
                    parent_handle,
                    source_name,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=create_output_immediately_before_install,
            ), self.assertRaises(render_skills.RenderTransactionError):
                render_skills.render_all(output_root=target)

            self.assertEqual(
                "last-moment output\n",
                (target / "valuable.txt").read_text(encoding="utf-8"),
            )
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(prior, self.snapshot(backups[0]))

    def test_absent_output_gaining_state_after_verification_is_not_displaced(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = parent / "generated"
            real_verify = render_skills.verify_output_root_state
            injected = False

            def create_output_after_absent_verification(
                candidate: Path,
                expected: render_skills.RenderOutputState,
            ) -> None:
                nonlocal injected
                real_verify(candidate, expected)
                if (
                    candidate.resolve(strict=False)
                    == target.resolve(strict=False)
                    and not expected.exists
                    and not injected
                ):
                    injected = True
                    target.mkdir()
                    (target / "valuable.txt").write_text(
                        "preserve newly concurrent output\n",
                        encoding="utf-8",
                    )

            with patch.object(
                render_skills,
                "verify_output_root_state",
                side_effect=create_output_after_absent_verification,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "concurrent state",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(injected)
            self.assertEqual(
                "preserve newly concurrent output\n",
                (target / "valuable.txt").read_text(encoding="utf-8"),
            )
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual([], backups)

    def test_rollback_no_replace_preserves_last_moment_output(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_rename = render_skills._rename_render_entry
            rename_count = 0

            def fail_install_then_race_rollback(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
            ) -> None:
                nonlocal rename_count
                rename_count += 1
                if rename_count == 2:
                    raise OSError("forced install failure")
                if rename_count == 3:
                    target.mkdir()
                    (target / "valuable.txt").write_text(
                        "last-moment rollback output\n",
                        encoding="utf-8",
                    )
                real_rename(
                    parent_handle,
                    source_name,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=fail_install_then_race_rollback,
            ), self.assertRaises(render_skills.RenderTransactionError):
                render_skills.render_all(output_root=target)

            self.assertEqual(
                "last-moment rollback output\n",
                (target / "valuable.txt").read_text(encoding="utf-8"),
            )
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(prior, self.snapshot(backups[0]))

    def test_existing_file_is_rejected_and_preserved(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = parent / "generated"
            target.write_text("unrelated file\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "render output root is not a directory",
            ):
                render_skills.render_all(output_root=target)

            self.assertEqual(
                "unrelated file\n",
                target.read_text(encoding="utf-8"),
            )
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_shared_directory_is_rejected_and_preserved(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = parent / "shared"
            target.mkdir()
            unrelated = target / "unrelated.md"
            unrelated.write_text("keep me\n", encoding="utf-8")
            before = self.snapshot(target)

            with self.assertRaisesRegex(
                ValueError,
                "non-dedicated render output root",
            ):
                render_skills.render_all(output_root=target)

            self.assertEqual(before, self.snapshot(target))
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_symlinked_output_root_is_rejected_and_preserved(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            shared = parent / "shared"
            shared.mkdir()
            unrelated = shared / "unrelated.md"
            unrelated.write_text("keep me\n", encoding="utf-8")
            target = parent / "generated"
            target.symlink_to(shared, target_is_directory=True)

            with self.assertRaisesRegex(
                ValueError,
                "symlinked render output root",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(target.is_symlink())
            self.assertEqual("keep me\n", unrelated.read_text(encoding="utf-8"))
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_symlinked_top_level_skill_root_is_rejected_without_mutation(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            external_skill = parent / "external-stata-core"
            skill_root = target / "stata-core"
            skill_root.rename(external_skill)
            skill_root.symlink_to(external_skill, target_is_directory=True)
            external_before = self.snapshot(external_skill)
            packages_before = self.snapshot(target / "stata-packages")

            with self.assertRaisesRegex(
                ValueError,
                "top-level skill root must be an ordinary directory",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(skill_root.is_symlink())
            self.assertEqual(external_before, self.snapshot(external_skill))
            self.assertEqual(
                packages_before,
                self.snapshot(target / "stata-packages"),
            )
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_target_replacement_during_backup_move_is_preserved(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            accepted = parent / "accepted-tree"
            concurrent = parent / "concurrent-tree"
            concurrent.mkdir()
            sentinel = concurrent / "unrelated.md"
            sentinel.write_text("concurrent bytes\n", encoding="utf-8")
            accepted_before = self.snapshot(target)
            real_replace = render_skills._rename_render_entry
            replaced = False

            def substitute_immediately_before_backup(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
            ) -> None:
                nonlocal replaced
                if (
                    not replaced
                    and source_name == target.name
                ):
                    replaced = True
                    target.rename(accepted)
                    concurrent.rename(target)
                real_replace(
                    parent_handle,
                    source_name,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=substitute_immediately_before_backup,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "accepted identity",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(replaced)
            self.assertEqual(accepted_before, self.snapshot(accepted))
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(
                "concurrent bytes\n",
                (backups[0] / "unrelated.md").read_text(encoding="utf-8"),
            )

    def test_nonstandard_in_repository_output_root_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            repository = Path(temp_root) / "repo"
            repository.mkdir()
            target = repository / "custom-generated"
            with patch.object(
                render_skills,
                "REPO_ROOT",
                repository,
            ), patch.object(
                render_skills,
                "BUILD_ROOT",
                repository / "build" / "generated",
            ), self.assertRaisesRegex(
                ValueError,
                "in-repository render output must be build/generated",
            ):
                render_skills.render_all(output_root=target)

            self.assertFalse(target.exists())

    def test_renderer_source_overlap_is_rejected_before_staging(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            repository = parent / "repo"
            repository.mkdir()
            build_root = repository / "build" / "generated"
            source_labels = (
                "config",
                "content",
                "templates",
                "locks",
            )
            relations = ("equal", "output-below", "source-below")

            for source_label in source_labels:
                for relation in relations:
                    with self.subTest(source=source_label, relation=relation):
                        case_root = parent / f"{source_label}-{relation}"
                        case_root.mkdir()
                        if relation == "equal":
                            output = case_root / "shared"
                            selected_source = output
                        elif relation == "output-below":
                            selected_source = case_root / "source"
                            output = selected_source / "generated"
                        else:
                            output = case_root / "generated"
                            selected_source = output / "source"

                        sources = {
                            "config": case_root / "independent-config.yaml",
                            "content": case_root / "independent-content",
                            "templates": case_root / "independent-templates",
                            "locks": case_root / "independent-locks",
                        }
                        sources[source_label] = selected_source

                        with patch.multiple(
                            render_skills,
                            REPO_ROOT=repository,
                            BUILD_ROOT=build_root,
                            TEMPLATES_ROOT=sources["templates"],
                            LOCK_ROOT=sources["locks"],
                        ), self.assertRaisesRegex(
                            ValueError,
                            "overlaps a renderer source",
                        ):
                            render_skills.render_all(
                                output_root=output,
                                content_root=sources["content"],
                                config_path=sources["config"],
                            )

                        self.assertFalse(output.exists())
                        self.assertEqual(
                            [],
                            list(case_root.rglob(f".{output.name}.stage-*")),
                        )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS case-alias behavior",
    )
    def test_case_insensitive_source_alias_overlap_is_rejected(self) -> None:
        with TemporaryDirectory(
            prefix=".atomic-render-case-",
            dir=repository_test_tmp_root(),
        ) as temp_root:
            parent = Path(temp_root)
            repository = parent / "repo"
            repository.mkdir()
            build_root = repository / "build" / "generated"

            for relation in ("equal", "output-below", "source-below"):
                with self.subTest(relation=relation):
                    case_root = parent / relation
                    case_root.mkdir()
                    if relation == "source-below":
                        output = case_root / "Output"
                        output.mkdir()
                        selected_source = case_root / "oUTPUT" / "content"
                        alias = case_root / "oUTPUT"
                    else:
                        selected_source = case_root / "Source"
                        selected_source.mkdir()
                        alias = case_root / "sOURCE"
                        output = (
                            alias
                            if relation == "equal"
                            else alias / "generated"
                        )
                    existing = output if relation == "source-below" else selected_source
                    if not alias.exists() or not os.path.samefile(existing, alias):
                        self.skipTest("test filesystem is case-sensitive")

                    config = case_root / "independent-config.yaml"
                    config.write_text("{}\n", encoding="utf-8")
                    templates = case_root / "independent-templates"
                    locks = case_root / "independent-locks"
                    templates.mkdir()
                    locks.mkdir()

                    with patch.multiple(
                        render_skills,
                        REPO_ROOT=repository,
                        BUILD_ROOT=build_root,
                        TEMPLATES_ROOT=templates,
                        LOCK_ROOT=locks,
                    ), self.assertRaisesRegex(
                        ValueError,
                        "overlaps a renderer source",
                    ):
                        render_skills.render_all(
                            output_root=output,
                            content_root=selected_source,
                            config_path=config,
                        )

                    self.assertEqual(
                        [],
                        list(case_root.rglob(f".{output.name}.stage-*")),
                    )

    @unittest.skipUnless(
        sys.platform == "darwin",
        "macOS case-alias behavior",
    )
    def test_case_insensitive_tracked_canonical_alias_is_rejected(self) -> None:
        with TemporaryDirectory(
            prefix=".atomic-render-case-",
            dir=repository_test_tmp_root(),
        ) as temp_root:
            repository = Path(temp_root) / "repo"
            repository.mkdir()
            build_root = repository / "build" / "generated"

            with patch.multiple(
                render_skills,
                REPO_ROOT=repository,
                BUILD_ROOT=build_root,
            ), patch.object(
                render_skills,
                "tracked_source_paths",
                return_value=(),
            ):
                render_skills.render_all(output_root=build_root)

            build_alias = repository / "Build"
            if (
                not build_alias.exists()
                or not os.path.samefile(build_root.parent, build_alias)
            ):
                self.skipTest("test filesystem is case-sensitive")

            tracked = Path("Build/generated/stata-core/SKILL.md")
            marker = repository / tracked
            marker.write_text(
                "# Modified force-tracked alias\n",
                encoding="utf-8",
            )
            before = self.snapshot(build_root)
            output_alias = repository / "BUILD" / "GENERATED"
            with patch.multiple(
                render_skills,
                REPO_ROOT=repository,
                BUILD_ROOT=build_root,
            ), patch.object(
                render_skills,
                "tracked_source_paths",
                return_value=(tracked,),
            ), patch.object(
                render_skills,
                "_render_tree",
                wraps=render_skills._render_tree,
            ) as render_tree, self.assertRaisesRegex(
                ValueError,
                "contains Git-tracked paths",
            ):
                render_skills.render_all(output_root=output_alias)

            render_tree.assert_not_called()
            self.assertEqual(before, self.snapshot(build_root))
            self.assertEqual(
                [],
                self.transaction_artifacts(build_root.parent, build_root.name),
            )

    def test_force_tracked_canonical_output_fails_before_rendering(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            repository = Path(temp_root) / "repo"
            repository.mkdir()
            build_root = repository / "build" / "generated"
            tracked = Path("build/generated/stata-core/SKILL.md")

            with patch.multiple(
                render_skills,
                REPO_ROOT=repository,
                BUILD_ROOT=build_root,
            ), patch.object(
                render_skills,
                "tracked_source_paths",
                return_value=(),
            ):
                render_skills.render_all(output_root=build_root)

            marker = build_root / "stata-core" / "SKILL.md"
            marker.write_text("# Modified force-tracked tree\n", encoding="utf-8")
            before = self.snapshot(build_root)
            with patch.multiple(
                render_skills,
                REPO_ROOT=repository,
                BUILD_ROOT=build_root,
            ), patch.object(
                render_skills,
                "tracked_source_paths",
                return_value=(tracked,),
            ), patch.object(
                render_skills,
                "_render_tree",
                wraps=render_skills._render_tree,
            ) as render_tree, self.assertRaisesRegex(
                ValueError,
                "contains Git-tracked paths",
            ):
                render_skills.render_all(output_root=build_root)

            render_tree.assert_not_called()
            self.assertEqual(before, self.snapshot(build_root))
            self.assertEqual(
                [],
                self.transaction_artifacts(build_root.parent, build_root.name),
            )

    def test_symlinked_build_ancestor_cannot_redirect_canonical_render(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            external_build = parent / "external-build"
            external_generated = external_build / "generated"
            render_skills.render_all(output_root=external_generated)
            external_marker = external_generated / "stata-core" / "SKILL.md"
            external_marker.write_text(
                "# External generated tree\n",
                encoding="utf-8",
            )
            external_before = self.snapshot(external_generated)

            repository = parent / "repo"
            repository.mkdir()
            (repository / "build").symlink_to(
                external_build,
                target_is_directory=True,
            )
            build_root = repository / "build" / "generated"

            with patch.object(
                render_skills,
                "REPO_ROOT",
                repository,
            ), patch.object(
                render_skills,
                "BUILD_ROOT",
                build_root,
            ), self.assertRaisesRegex(
                ValueError,
                "symbolic-link or non-directory component",
            ):
                render_skills.render_all(output_root=build_root)

            self.assertEqual(external_before, self.snapshot(external_generated))
            self.assertEqual(
                [],
                self.transaction_artifacts(external_build, "generated"),
            )

    def test_unsafe_render_paths_fail_before_staging_or_external_writes(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            base_config = yaml.safe_load(
                (REPO_ROOT / "config" / "skills.yaml").read_text(encoding="utf-8")
            )
            content_root = parent / "content"
            shutil.copytree(REPO_ROOT / "content", content_root)

            cases: list[tuple[str, dict, Path]] = []

            unsafe_alias = yaml.safe_load(yaml.safe_dump(base_config))
            unsafe_alias["route_aliases"][0]["from_route"] = "../../escaped.md"
            cases.append(("from-route", unsafe_alias, content_root))

            unsafe_route_dir = yaml.safe_load(yaml.safe_dump(base_config))
            unsafe_route_dir["skills"]["core"]["route_dir"] = "../../references"
            cases.append(("route-dir", unsafe_route_dir, content_root))

            unsafe_slug_root = parent / "unsafe-slug-content"
            shutil.copytree(content_root, unsafe_slug_root)
            slug_path = unsafe_slug_root / "core" / "panel-data.yaml"
            slug_entry = yaml.safe_load(slug_path.read_text(encoding="utf-8"))
            slug_entry["slug"] = "../../../escaped"
            slug_path.write_text(
                yaml.safe_dump(slug_entry, sort_keys=False),
                encoding="utf-8",
            )
            cases.append(("slug", base_config, unsafe_slug_root))

            for label, config, selected_content_root in cases:
                with self.subTest(label=label):
                    case_root = parent / label
                    case_root.mkdir()
                    escaped = case_root / "escaped.md"
                    escaped.write_text("sentinel\n", encoding="utf-8")
                    config_path = case_root / "skills.yaml"
                    config_path.write_text(
                        yaml.safe_dump(config, sort_keys=False),
                        encoding="utf-8",
                    )
                    output = case_root / "generated"

                    with self.assertRaisesRegex(
                        ValueError,
                        "render input validation failed",
                    ):
                        render_skills.render_all(
                            output_root=output,
                            content_root=selected_content_root,
                            config_path=config_path,
                        )

                    self.assertEqual(
                        "sentinel\n",
                        escaped.read_text(encoding="utf-8"),
                    )
                    self.assertFalse(output.exists())
                    self.assertEqual(
                        [],
                        self.transaction_artifacts(case_root, output.name),
                    )

    def test_duplicate_config_key_fails_during_input_capture(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            config_path = Path(temp_root) / "skills.yaml"
            config_path.write_text(
                (
                    REPO_ROOT / "config" / "skills.yaml"
                ).read_text(encoding="utf-8")
                + "\nskills: {}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                r"duplicate key 'skills'.*first occurrence was at line",
            ) as caught:
                render_skills.capture_render_inputs(
                    config_path,
                    REPO_ROOT / "content",
                )

            self.assertIn(str(config_path), str(caught.exception))

    def test_invalid_content_directory_is_rejected_before_discovery(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            root = Path(temp_root)
            content_root = root / "content"
            shutil.copytree(REPO_ROOT / "content", content_root)
            outside = root / "outside"
            outside.mkdir()
            outside_yaml = outside / "valuable.yaml"
            outside_yaml.write_text(
                "title: preserve outside bytes\n",
                encoding="utf-8",
            )
            config = yaml.safe_load(
                (REPO_ROOT / "config" / "skills.yaml").read_text(
                    encoding="utf-8"
                )
            )
            config["skills"]["core"]["content_dir"] = "../outside"
            config_path = root / "skills.yaml"
            config_path.write_text(
                yaml.safe_dump(config, sort_keys=False),
                encoding="utf-8",
            )
            captured_paths: list[Path] = []
            real_capture = render_skills._capture_render_input_file

            def record_capture(path: Path) -> render_skills.RenderInputFile:
                captured_paths.append(Path(path))
                return real_capture(path)

            target = root / "output" / "generated"
            with patch.object(
                render_skills,
                "_capture_render_input_file",
                side_effect=record_capture,
            ), self.assertRaisesRegex(
                ValueError,
                "content_dir must be one relative component",
            ):
                render_skills.render_all(
                    output_root=target,
                    content_root=content_root,
                    config_path=config_path,
                )

            self.assertNotIn(outside_yaml, captured_paths)
            self.assertFalse(target.parent.exists())
            self.assertEqual(
                "title: preserve outside bytes\n",
                outside_yaml.read_text(encoding="utf-8"),
            )

    def test_content_change_after_validation_cannot_replace_prior_tree(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            content_root = parent / "content"
            shutil.copytree(REPO_ROOT / "content", content_root)
            changed_path = content_root / "core" / "panel-data.yaml"
            real_validate = render_skills.validate_render_inputs
            changed = False

            def validate_then_change_content(
                config: dict,
                entries: tuple[render_skills.RenderContentEntry, ...],
            ) -> None:
                nonlocal changed
                real_validate(config, entries)
                entry = yaml.safe_load(
                    changed_path.read_text(encoding="utf-8")
                )
                entry["title"] = ""
                changed_path.write_text(
                    yaml.safe_dump(entry, sort_keys=False),
                    encoding="utf-8",
                )
                changed = True

            with patch.object(
                render_skills,
                "validate_render_inputs",
                side_effect=validate_then_change_content,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "render inputs changed after validation",
            ):
                render_skills.render_all(
                    output_root=target,
                    content_root=content_root,
                )

            self.assertTrue(changed)
            self.assertEqual(prior, self.snapshot(target))
            self.assertEqual(
                [],
                self.transaction_artifacts(parent, target.name),
            )

    def test_input_change_after_placement_restores_prior_tree(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            content_root = parent / "content"
            shutil.copytree(REPO_ROOT / "content", content_root)
            changed_path = content_root / "core" / "panel-data.yaml"
            real_verify = render_skills.verify_render_inputs
            verify_count = 0

            def change_after_final_preplacement_check(
                snapshot: render_skills.RenderInputSnapshot,
            ) -> None:
                nonlocal verify_count
                verify_count += 1
                real_verify(snapshot)
                if verify_count == 4:
                    entry = yaml.safe_load(
                        changed_path.read_text(encoding="utf-8")
                    )
                    entry["title"] = "Changed after placement checkpoint"
                    changed_path.write_text(
                        yaml.safe_dump(entry, sort_keys=False),
                        encoding="utf-8",
                    )

            with patch.object(
                render_skills,
                "verify_render_inputs",
                side_effect=change_after_final_preplacement_check,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "placed staged tree failed",
            ) as raised:
                render_skills.render_all(
                    output_root=target,
                    content_root=content_root,
                )

            self.assertGreaterEqual(verify_count, 5)
            self.assertIn(
                "render inputs changed after validation",
                str(raised.exception.__cause__),
            )
            self.assertEqual(prior, self.snapshot(target))
            recoveries = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".recovery-" in path.name
            ]
            self.assertEqual(1, len(recoveries))

    def test_parent_change_after_success_cleanup_cannot_report_success(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            root = Path(temp_root)
            approved_parent = root / "approved-parent"
            approved_parent.mkdir()
            target = self.seeded_target(approved_parent)
            displaced_parent = root / "displaced-parent"
            real_cleanup = render_skills._remove_verified_backup
            changed = False

            def cleanup_then_change_parent(
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal changed
                real_cleanup(*args, **kwargs)
                approved_parent.rename(displaced_parent)
                approved_parent.mkdir()
                (approved_parent / "sentinel.txt").write_text(
                    "replacement parent bytes\n",
                    encoding="utf-8",
                )
                changed = True

            with patch.object(
                render_skills,
                "_remove_verified_backup",
                side_effect=cleanup_then_change_parent,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "prior tree was verified and removed.*parent changed",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(changed)
            self.assertEqual(
                "replacement parent bytes\n",
                (approved_parent / "sentinel.txt").read_text(encoding="utf-8"),
            )
            self.assertTrue((displaced_parent / target.name).is_dir())
            self.assertFalse(target.exists())
            self.assertEqual(
                [],
                self.transaction_artifacts(
                    displaced_parent,
                    target.name,
                ),
            )


class CallerWorkspacePreservationTests(unittest.TestCase):
    def test_successful_determinism_check_removes_workspace(self) -> None:
        with TemporaryDirectory(prefix="determinism-success-test-") as temp_root:
            workspace = Path(temp_root) / "workspace"
            workspace.mkdir(mode=0o700)
            output = io.StringIO()

            with patch.object(
                check_determinism.tempfile,
                "mkdtemp",
                return_value=str(workspace),
            ), redirect_stdout(output):
                result = check_determinism.main()

            self.assertEqual(0, result)
            self.assertFalse(workspace.exists())
            self.assertIn(
                "Deterministic double render passed",
                output.getvalue(),
            )
            self.assertNotIn("retained", output.getvalue().lower())

    def test_successful_generated_drift_check_removes_workspace(self) -> None:
        with TemporaryDirectory(prefix="drift-success-test-") as temp_root:
            fixture_root = Path(temp_root)
            build_root = fixture_root / "current-generated"
            render_skills.render_all(output_root=build_root)
            workspace = fixture_root / "workspace"
            workspace.mkdir(mode=0o700)
            output = io.StringIO()

            with patch.object(
                lint_skill_pack,
                "BUILD_ROOT",
                build_root,
            ), patch.object(
                lint_skill_pack.tempfile,
                "mkdtemp",
                return_value=str(workspace),
            ), redirect_stdout(output):
                errors = lint_skill_pack.lint_generated_drift()

            self.assertEqual([], errors)
            self.assertFalse(workspace.exists())
            self.assertNotIn("retained", output.getvalue().lower())

    def test_late_former_stage_reappearance_never_names_live_output_for_cleanup(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="former-stage-reappearance-test-")
        )
        target = fixture_root / "generated"
        real_replace = render_skills._replace_rendered_tree
        reappeared_stage: Path | None = None
        sentinel: Path | None = None

        def replace_then_reappear(
            parent: render_skills.RenderParentHandle,
            staged_root: Path,
            output_root: Path,
            expected_output_state: render_skills.RenderOutputState,
            expected_staged_state: render_skills.RenderOutputState,
            validate_staged_state,
            verify_current_inputs,
        ) -> None:
            nonlocal reappeared_stage, sentinel
            real_replace(
                parent,
                staged_root,
                output_root,
                expected_output_state,
                expected_staged_state,
                validate_staged_state,
                verify_current_inputs,
            )
            staged_root.mkdir(mode=0o700)
            reappeared_stage = staged_root
            sentinel = staged_root / "valuable.txt"
            sentinel.write_text(
                "preserve unrelated former-stage content\n",
                encoding="utf-8",
            )

        output = io.StringIO()
        try:
            with patch.object(
                render_skills,
                "_replace_rendered_tree",
                side_effect=replace_then_reappear,
            ), redirect_stdout(output):
                render_skills.render_all(output_root=target)

            self.assertEqual(
                {"stata-core", "stata-packages", "stata-c-plugins"},
                {path.name for path in target.iterdir() if path.is_dir()},
            )
            self.assertIsNotNone(reappeared_stage)
            self.assertIsNotNone(sentinel)
            assert reappeared_stage is not None
            assert sentinel is not None
            self.assertEqual(
                "preserve unrelated former-stage content\n",
                sentinel.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                {"valuable.txt"},
                {path.name for path in reappeared_stage.iterdir()},
            )
            rendered_output = output.getvalue()
            self.assertIn(
                "former render stage name contains unverified state and was "
                "left unchanged",
                rendered_output,
            )
            self.assertIn(str(reappeared_stage), rendered_output)
            cleanup_lines = [
                line
                for line in rendered_output.splitlines()
                if (
                    "explicit cleanup at:" in line.lower()
                    or "retained stage location:" in line.lower()
                )
            ]
            for line in cleanup_lines:
                self.assertNotIn(str(target), line)
        finally:
            shutil.rmtree(fixture_root)

    def test_determinism_reports_cross_parent_workspace_move(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="determinism-cross-parent-test-")
        )
        original_parent = fixture_root / "original"
        moved_parent = fixture_root / "moved"
        original_parent.mkdir()
        moved_parent.mkdir()
        retained_root = original_parent / "retained-workspace"
        retained_root.mkdir(mode=0o700)
        moved_root = moved_parent / "retained-workspace"
        render_error = RuntimeError("forced cross-parent deterministic render")

        def move_after_partial_render(*, output_root: Path) -> None:
            output_root.mkdir(parents=True)
            (output_root / "partial.txt").write_text(
                "preserve cross-parent deterministic render\n",
                encoding="utf-8",
            )
            retained_root.rename(moved_root)
            raise render_error

        output = io.StringIO()
        try:
            with patch.object(
                check_determinism.tempfile,
                "mkdtemp",
                return_value=str(retained_root),
            ), patch.object(
                check_determinism,
                "render_all",
                side_effect=move_after_partial_render,
            ), redirect_stdout(output), self.assertRaises(RuntimeError) as raised:
                check_determinism.main()

            self.assertIs(render_error, raised.exception)
            self.assertFalse(retained_root.exists())
            self.assertEqual(
                "preserve cross-parent deterministic render\n",
                (moved_root / "first" / "partial.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                "deterministic-render workspace retained for explicit cleanup "
                f"at: {moved_root.resolve()}",
                output.getvalue(),
            )
            self.assertNotIn(
                "unknown pathname",
                output.getvalue(),
            )
        finally:
            shutil.rmtree(fixture_root)

    def test_generated_drift_reports_cross_parent_workspace_move(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="drift-cross-parent-test-")
        )
        original_parent = fixture_root / "original"
        moved_parent = fixture_root / "moved"
        original_parent.mkdir()
        moved_parent.mkdir()
        retained_root = original_parent / "retained-workspace"
        retained_root.mkdir(mode=0o700)
        moved_root = moved_parent / "retained-workspace"
        build_root = fixture_root / "current-generated"
        build_root.mkdir()

        def move_after_partial_render(*, output_root: Path) -> None:
            output_root.mkdir(parents=True)
            (output_root / "partial.txt").write_text(
                "preserve cross-parent drift render\n",
                encoding="utf-8",
            )
            retained_root.rename(moved_root)
            raise RuntimeError("forced cross-parent generated-drift render")

        output = io.StringIO()
        try:
            with patch.object(
                lint_skill_pack,
                "BUILD_ROOT",
                build_root,
            ), patch.object(
                lint_skill_pack.tempfile,
                "mkdtemp",
                return_value=str(retained_root),
            ), patch.object(
                render_skills,
                "render_all",
                side_effect=move_after_partial_render,
            ), redirect_stdout(output):
                errors = lint_skill_pack.lint_generated_drift()

            self.assertEqual(
                [
                    "generated render failed: "
                    "forced cross-parent generated-drift render"
                ],
                errors,
            )
            self.assertFalse(retained_root.exists())
            self.assertEqual(
                "preserve cross-parent drift render\n",
                (moved_root / "generated" / "partial.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn(
                "generated-drift workspace retained for explicit cleanup "
                f"at: {moved_root.resolve()}",
                output.getvalue(),
            )
            self.assertNotIn(
                "unknown pathname",
                output.getvalue(),
            )
        finally:
            shutil.rmtree(fixture_root)


if __name__ == "__main__":
    unittest.main()
