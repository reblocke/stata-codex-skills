from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import render_skills  # noqa: E402


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

    @staticmethod
    def seeded_target(parent: Path) -> Path:
        target = parent / "generated"
        render_skills.render_all(output_root=target)
        (target / "stata-core" / "SKILL.md").write_text(
            "# Prior generated tree\n",
            encoding="utf-8",
        )
        return target

    def test_success_replaces_complete_tree_and_cleans_transaction_artifacts(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)

            render_skills.render_all(output_root=target)

            self.assertNotEqual(
                b"# Prior generated tree\n",
                (target / "stata-core" / "SKILL.md").read_bytes(),
            )
            self.assertEqual(
                {"stata-core", "stata-packages", "stata-c-plugins"},
                {path.name for path in target.iterdir() if path.is_dir()},
            )
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

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
                staged_root: Path,
                _output_root: Path,
                _expected: render_skills.RenderOutputState,
                _expected_staged: render_skills.RenderOutputState,
                _validator: object,
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
            surviving_values = [
                path.read_text(encoding="utf-8")
                for path in parent.rglob("valuable.txt")
            ]
            self.assertEqual(["preserve stage-path bytes\n"], surviving_values)
            self.assertTrue(displaced_stage.is_dir())

    def test_substituted_staged_tree_after_validation_is_not_committed(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            validated_stage = parent / "validated-stage"
            real_replace = render_skills._replace_rendered_tree

            def substitute_before_transaction(
                staged_root: Path,
                output_root: Path,
                expected_output: render_skills.RenderOutputState,
                expected_staged: render_skills.RenderOutputState,
                validator: object,
            ) -> None:
                staged_root.rename(validated_stage)
                shutil.copytree(validated_stage, staged_root)
                (staged_root / "stata-core" / "SKILL.md").write_text(
                    "# Unvalidated replacement\n",
                    encoding="utf-8",
                )
                real_replace(
                    staged_root,
                    output_root,
                    expected_output,
                    expected_staged,
                    validator,
                )

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_replace_rendered_tree",
                side_effect=substitute_before_transaction,
            ), redirect_stdout(output), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "changed after preflight",
            ):
                render_skills.render_all(output_root=target)

            self.assertEqual(prior, self.snapshot(target))
            self.assertTrue(validated_stage.is_dir())
            substituted_stages = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".stage-" in path.name
            ]
            self.assertEqual(1, len(substituted_stages))
            self.assertEqual(
                "# Unvalidated replacement\n",
                (
                    substituted_stages[0] / "stata-core" / "SKILL.md"
                ).read_text(encoding="utf-8"),
            )
            self.assertIn("staged render cleanup was skipped", output.getvalue())

    def test_identity_stable_stage_mutation_is_preserved(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)

            def mutate_stage_then_fail(
                staged_root: Path,
                _output_root: Path,
                _expected_output: render_skills.RenderOutputState,
                _expected_staged: render_skills.RenderOutputState,
                _validator: object,
            ) -> None:
                foreign = staged_root / "foreign-dir"
                foreign.mkdir()
                (foreign / "valuable.txt").write_text(
                    "preserve same-inode stage bytes\n",
                    encoding="utf-8",
                )
                raise RuntimeError("forced failure after stage mutation")

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_replace_rendered_tree",
                side_effect=mutate_stage_then_fail,
            ), redirect_stdout(output), self.assertRaisesRegex(
                RuntimeError,
                "forced failure after stage mutation",
            ):
                render_skills.render_all(output_root=target)

            self.assertEqual(prior, self.snapshot(target))
            stages = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".stage-" in path.name
            ]
            self.assertEqual(1, len(stages))
            self.assertEqual(
                "preserve same-inode stage bytes\n",
                (stages[0] / "foreign-dir" / "valuable.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertIn("staged render cleanup was skipped", output.getvalue())

    def test_stage_substitution_inside_install_is_quarantined_and_rolled_back(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            validated_stage = parent / "validated-stage"
            real_rename = render_skills._atomic_rename_no_replace
            rename_count = 0

            def substitute_during_install(
                source: Path,
                destination: Path,
            ) -> None:
                nonlocal rename_count
                rename_count += 1
                if rename_count == 2:
                    source.rename(validated_stage)
                    shutil.copytree(validated_stage, source)
                    (source / "stata-core" / "SKILL.md").write_text(
                        "# Unvalidated placement\n",
                        encoding="utf-8",
                    )
                real_rename(source, destination)

            with patch.object(
                render_skills,
                "_atomic_rename_no_replace",
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
            self.assertFalse((stages[0] / "stata-core" / "SKILL.md").exists())
            self.assertIn(
                "no trusted entry manifest was captured",
                output.getvalue(),
            )

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
            real_replace = render_skills._atomic_rename_no_replace
            replace_count = 0

            def fail_new_tree_swap(source: Path, destination: Path) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count == 2:
                    raise OSError("forced swap failure")
                real_replace(source, destination)

            with patch.object(
                render_skills,
                "_atomic_rename_no_replace",
                side_effect=fail_new_tree_swap,
            ):
                with self.assertRaisesRegex(OSError, "forced swap failure"):
                    render_skills.render_all(output_root=target)

            self.assertEqual(3, replace_count)
            self.assertEqual(before, self.snapshot(target))
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_keyboard_interrupt_after_backup_move_restores_previous_tree(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_verify = render_skills._verify_moved_output_state
            interrupted = False

            def interrupt_after_backup_move(
                backup: Path,
                expected: render_skills.RenderOutputState,
            ) -> None:
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt("forced interrupt after backup move")
                real_verify(backup, expected)

            with patch.object(
                render_skills,
                "_verify_moved_output_state",
                side_effect=interrupt_after_backup_move,
            ), self.assertRaisesRegex(
                KeyboardInterrupt,
                "forced interrupt after backup move",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(interrupted)
            self.assertEqual(prior, self.snapshot(target))
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_interrupt_with_concurrent_output_preserves_backup_and_reports_recovery(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            interrupted = False

            def interrupt_after_concurrent_output(
                _backup: Path,
                _expected: render_skills.RenderOutputState,
            ) -> None:
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    target.mkdir()
                    (target / "valuable.txt").write_text(
                        "preserve concurrent output\n",
                        encoding="utf-8",
                    )
                    raise KeyboardInterrupt(
                        "forced interrupt with concurrent output"
                    )

            with patch.object(
                render_skills,
                "_verify_moved_output_state",
                side_effect=interrupt_after_concurrent_output,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "concurrent state",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(interrupted)
            self.assertEqual(
                "preserve concurrent output\n",
                (target / "valuable.txt").read_text(encoding="utf-8"),
            )
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(prior, self.snapshot(backups[0]))

    def test_failed_restore_preserves_prior_tree_at_reported_backup(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)
            real_replace = render_skills._atomic_rename_no_replace
            replace_count = 0

            def fail_swap_and_restore(source: Path, destination: Path) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count >= 2:
                    raise OSError(f"forced replace failure {replace_count}")
                real_replace(source, destination)

            with patch.object(
                render_skills,
                "_atomic_rename_no_replace",
                side_effect=fail_swap_and_restore,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "prior tree remains at",
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

    def test_backup_cleanup_failure_reports_warning_after_successful_commit(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)

            def fail_backup_cleanup(
                path: Path,
                expected: render_skills.RenderOutputState,
            ) -> None:
                raise PermissionError("forced backup cleanup failure")

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_remove_verified_backup",
                side_effect=fail_backup_cleanup,
            ), redirect_stdout(output):
                render_skills.render_all(output_root=target)

            self.assertIn("rendered tree was committed", output.getvalue())
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(before, self.snapshot(backups[0]))

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
            ) -> None:
                nonlocal substituted
                substituted = True
                backup.rename(accepted_backup)
                concurrent.rename(backup)
                real_remove(backup, expected)

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_remove_verified_backup",
                side_effect=substitute_before_cleanup,
            ), redirect_stdout(output):
                render_skills.render_all(output_root=target)

            self.assertTrue(substituted)
            self.assertIn("backup could not be removed", output.getvalue())
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
            real_clear = render_skills._clear_directory_descriptor
            injected = False

            def add_entry_after_backup_verification(
                descriptor: int,
                display_path: Path,
                expected_entries: dict[
                    str,
                    render_skills.RenderTreeEntry,
                ]
                | None = None,
                relative_parts: tuple[str, ...] = (),
            ) -> None:
                nonlocal injected
                if not injected and expected_entries is not None:
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
                real_clear(
                    descriptor,
                    display_path,
                    expected_entries,
                    relative_parts,
                )

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_clear_directory_descriptor",
                side_effect=add_entry_after_backup_verification,
            ), redirect_stdout(output):
                render_skills.render_all(output_root=target)

            self.assertTrue(injected)
            self.assertIn("backup could not be removed", output.getvalue())
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

    def test_backup_cleanup_quarantines_replacement_before_deletion(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-race"
            backup.mkdir()
            accepted = backup / "owned.txt"
            accepted.write_text("accepted generated bytes\n", encoding="utf-8")
            metadata = backup.stat()
            expected_entries = render_skills._capture_tree_entries(backup)
            real_rename = render_skills._atomic_rename_at_no_replace
            raced = False

            def replace_after_stat_before_quarantine(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal raced
                if source_name == "owned.txt" and not raced:
                    raced = True
                    os.rename(
                        source_name,
                        "accepted-moved.txt",
                        src_dir_fd=source_descriptor,
                        dst_dir_fd=source_descriptor,
                    )
                    file_descriptor = os.open(
                        source_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=source_descriptor,
                    )
                    try:
                        os.write(
                            file_descriptor,
                            b"foreign replacement bytes\n",
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
                side_effect=replace_after_stat_before_quarantine,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "changed while it was moved into private quarantine",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertTrue(raced)
            self.assertEqual(
                "foreign replacement bytes\n",
                (backup / "owned.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "accepted generated bytes\n",
                (backup / "accepted-moved.txt").read_text(encoding="utf-8"),
            )

    def test_double_failure_reports_surviving_quarantine_path(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            owned = parent / "owned"
            owned.mkdir()
            (owned / "accepted.txt").write_text(
                "accepted quarantined bytes\n",
                encoding="utf-8",
            )
            different_entry = parent / "different-entry"
            different_entry.mkdir()
            expected = different_entry.stat()
            parent_descriptor = os.open(
                parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            real_rename = render_skills._atomic_rename_at_no_replace
            move_count = 0

            def fail_move_then_fail_restore(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal move_count
                if source_name != "owned":
                    real_rename(
                        source_descriptor,
                        source_name,
                        destination_descriptor,
                        destination_name,
                    )
                    return
                move_count += 1
                if move_count == 1:
                    real_rename(
                        source_descriptor,
                        source_name,
                        destination_descriptor,
                        destination_name,
                    )
                    return
                os.mkdir(destination_name, dir_fd=destination_descriptor)
                (parent / destination_name / "concurrent.txt").write_text(
                    "concurrent destination bytes\n",
                    encoding="utf-8",
                )
                raise OSError("forced restoration failure")

            try:
                with patch.object(
                    render_skills,
                    "_atomic_rename_at_no_replace",
                    side_effect=fail_move_then_fail_restore,
                ), self.assertRaises(
                    render_skills.RenderTransactionError,
                ) as raised:
                    render_skills._remove_verified_empty_directory_via_quarantine(
                        parent_descriptor,
                        owned.name,
                        owned,
                        expected,
                    )
            finally:
                os.close(parent_descriptor)

            cleanup_roots = sorted(parent.glob(".render-cleanup-*"))
            self.assertEqual(2, move_count)
            self.assertEqual(1, len(cleanup_roots))
            surviving_path = cleanup_roots[0] / owned.name
            self.assertIn(str(surviving_path), str(raised.exception))
            self.assertTrue(surviving_path.is_dir())
            self.assertEqual(
                "accepted quarantined bytes\n",
                (surviving_path / "accepted.txt").read_text(encoding="utf-8"),
            )

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

    def test_backup_cleanup_rehashes_immediately_before_file_deletion(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-final-hash-race"
            backup.mkdir()
            accepted = backup / "owned.txt"
            accepted.write_text("accepted generated bytes\n", encoding="utf-8")
            metadata = backup.stat()
            expected_entries = render_skills._capture_tree_entries(backup)
            real_hash = render_skills._sha256_regular_file_no_follow
            quarantine_hashes = 0
            mutated = False

            def mutate_after_first_child_quarantine_hash(
                name: str | Path,
                *,
                dir_fd: int | None = None,
                expected_metadata: os.stat_result | None = None,
            ) -> str:
                nonlocal mutated, quarantine_hashes
                digest = real_hash(
                    name,
                    dir_fd=dir_fd,
                    expected_metadata=expected_metadata,
                )
                if dir_fd is not None and os.fspath(name) == "owned.txt":
                    quarantine_hashes += 1
                    # The first hash is the complete-tree verification after
                    # the root move. Mutate after the first child-quarantine
                    # hash so the final immediate pre-delete hash must catch it.
                    if quarantine_hashes == 2:
                        descriptor = os.open(
                            name,
                            os.O_WRONLY | os.O_APPEND,
                            dir_fd=dir_fd,
                        )
                        try:
                            os.write(
                                descriptor,
                                b"foreign post-hash bytes\n",
                            )
                        finally:
                            os.close(descriptor)
                        mutated = True
                return digest

            with patch.object(
                render_skills,
                "_sha256_regular_file_no_follow",
                side_effect=mutate_after_first_child_quarantine_hash,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "immediately before deletion.*preserved",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertTrue(mutated)
            self.assertGreaterEqual(quarantine_hashes, 3)
            self.assertEqual(
                "accepted generated bytes\nforeign post-hash bytes\n",
                accepted.read_text(encoding="utf-8"),
            )

    def test_empty_backup_directory_replacement_before_private_move_is_preserved(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-empty-directory-race"
            accepted_name = "accepted-empty-directory"
            backup.mkdir()
            (backup / "owned-empty").mkdir()
            metadata = backup.stat()
            expected_entries = render_skills._capture_tree_entries(backup)
            real_rename = render_skills._atomic_rename_at_no_replace
            owned_move_count = 0
            replacement_identity: tuple[int, int] | None = None

            def replace_empty_directory_before_private_move(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal owned_move_count, replacement_identity
                if source_name == "owned-empty":
                    owned_move_count += 1
                    if owned_move_count == 2:
                        os.rename(
                            source_name,
                            accepted_name,
                            src_dir_fd=source_descriptor,
                            dst_dir_fd=source_descriptor,
                        )
                        os.mkdir(
                            source_name,
                            0o700,
                            dir_fd=source_descriptor,
                        )
                        replacement = os.stat(
                            source_name,
                            dir_fd=source_descriptor,
                            follow_symlinks=False,
                        )
                        replacement_identity = (
                            replacement.st_dev,
                            replacement.st_ino,
                        )
                real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_atomic_rename_at_no_replace",
                side_effect=replace_empty_directory_before_private_move,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "changed while moving into private cleanup quarantine.*preserved",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertEqual(3, owned_move_count)
            self.assertIsNotNone(replacement_identity)
            accepted = next(backup.rglob(accepted_name))
            replacement_path = accepted.parent / "owned-empty"
            self.assertTrue(accepted.is_dir())
            self.assertTrue(replacement_path.is_dir())
            replacement = replacement_path.stat()
            self.assertEqual(
                replacement_identity,
                (replacement.st_dev, replacement.st_ino),
            )

    def test_backup_root_replacement_before_private_move_is_preserved(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-root-race"
            accepted = parent / "accepted-backup-root"
            backup.mkdir()
            metadata = backup.stat()
            real_rename = render_skills._atomic_rename_at_no_replace
            replacement_identity: tuple[int, int] | None = None

            def replace_root_before_private_move(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal replacement_identity
                if (
                    source_name == backup.name
                    and replacement_identity is None
                ):
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
                    replacement = os.stat(
                        source_name,
                        dir_fd=source_descriptor,
                        follow_symlinks=False,
                    )
                    replacement_identity = (
                        replacement.st_dev,
                        replacement.st_ino,
                    )
                real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_atomic_rename_at_no_replace",
                side_effect=replace_root_before_private_move,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "changed while moving into private cleanup quarantine.*preserved",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    (),
                )

            self.assertIsNotNone(replacement_identity)
            self.assertTrue(accepted.is_dir())
            self.assertTrue(backup.is_dir())
            replacement = backup.stat()
            self.assertEqual(
                replacement_identity,
                (replacement.st_dev, replacement.st_ino),
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

    def test_backup_root_reappearance_after_private_move_is_reported(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-post-move-race"
            backup.mkdir()
            metadata = backup.stat()
            real_verify = render_skills._verify_directory_descriptor_tree
            injected = False
            marker = backup / "foreign-after-move.txt"

            def reappear_after_private_verification(
                descriptor: int,
                display_path: Path,
                expected_entries: dict[
                    str,
                    render_skills.RenderTreeEntry,
                ],
                relative_parts: tuple[str, ...] = (),
            ) -> None:
                nonlocal injected
                real_verify(
                    descriptor,
                    display_path,
                    expected_entries,
                    relative_parts,
                )
                if not injected and not relative_parts:
                    backup.mkdir(mode=0o700)
                    marker.write_text(
                        "preserve post-move render bytes\n",
                        encoding="utf-8",
                    )
                    injected = True

            with patch.object(
                render_skills,
                "_verify_directory_descriptor_tree",
                side_effect=reappear_after_private_verification,
            ), patch.object(
                render_skills,
                "_clear_directory_descriptor",
                wraps=render_skills._clear_directory_descriptor,
            ) as clear_directory, self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "public name reappeared before private recursive cleanup",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    (),
                )

            self.assertTrue(injected)
            clear_directory.assert_not_called()
            self.assertEqual(
                "preserve post-move render bytes\n",
                marker.read_text(encoding="utf-8"),
            )
            cleanup_roots = [
                path
                for path in parent.iterdir()
                if path.name.startswith(render_skills.PRIVATE_CLEANUP_PREFIX)
            ]
            self.assertEqual(1, len(cleanup_roots))
            self.assertTrue((cleanup_roots[0] / backup.name).is_dir())

    def test_late_cleanup_sibling_preserves_complete_private_tree(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-late-cleanup-sibling"
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
            real_verify = render_skills._verify_directory_descriptor_tree
            late_sibling = parent / (
                f"{render_skills.PRIVATE_CLEANUP_PREFIX}late-sibling"
            )
            injected = False

            def add_sibling_after_private_verification(
                descriptor: int,
                display_path: Path,
                entries: dict[str, render_skills.RenderTreeEntry],
                relative_parts: tuple[str, ...] = (),
            ) -> None:
                nonlocal injected
                real_verify(
                    descriptor,
                    display_path,
                    entries,
                    relative_parts,
                )
                if not injected and not relative_parts:
                    late_sibling.mkdir(mode=0o700)
                    (late_sibling / "foreign.txt").write_text(
                        "foreign sibling bytes\n",
                        encoding="utf-8",
                    )
                    injected = True

            real_unlink = os.unlink
            with patch.object(
                render_skills,
                "_verify_directory_descriptor_tree",
                side_effect=add_sibling_after_private_verification,
            ), patch.object(
                render_skills,
                "_clear_directory_descriptor",
                wraps=render_skills._clear_directory_descriptor,
            ) as clear_directory, patch.object(
                render_skills.os,
                "unlink",
                wraps=real_unlink,
            ) as unlink, self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "unexpected private render cleanup sibling appeared",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertTrue(injected)
            clear_directory.assert_not_called()
            unlink.assert_not_called()
            self.assertEqual(
                "foreign sibling bytes\n",
                (late_sibling / "foreign.txt").read_text(encoding="utf-8"),
            )
            accepted_roots = [
                path / backup.name
                for path in parent.iterdir()
                if (
                    path.name.startswith(
                        render_skills.PRIVATE_CLEANUP_PREFIX
                    )
                    and path != late_sibling
                    and (path / backup.name).is_dir()
                )
            ]
            self.assertEqual(1, len(accepted_roots))
            self.assertEqual(
                0o700,
                accepted_roots[0].parent.stat().st_mode & 0o777,
            )
            self.assertEqual(
                accepted_snapshot,
                self.snapshot(accepted_roots[0]),
            )
            self.assertTrue((accepted_roots[0] / "nested").is_dir())

    def test_top_level_cleanup_substitution_preserves_complete_private_tree(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-cleanup-substitution"
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
            displaced_cleanup = parent / "displaced-private-cleanup"
            replacement_marker: Path | None = None
            real_verify = render_skills._verify_directory_descriptor_tree

            def substitute_cleanup_after_private_verification(
                descriptor: int,
                display_path: Path,
                entries: dict[str, render_skills.RenderTreeEntry],
                relative_parts: tuple[str, ...] = (),
            ) -> None:
                nonlocal replacement_marker
                real_verify(
                    descriptor,
                    display_path,
                    entries,
                    relative_parts,
                )
                if replacement_marker is None and not relative_parts:
                    cleanup_path = display_path.parent
                    cleanup_path.rename(displaced_cleanup)
                    cleanup_path.mkdir(mode=0o700)
                    replacement_marker = cleanup_path / "foreign.txt"
                    replacement_marker.write_text(
                        "foreign replacement cleanup bytes\n",
                        encoding="utf-8",
                    )

            real_unlink = os.unlink
            with patch.object(
                render_skills,
                "_verify_directory_descriptor_tree",
                side_effect=substitute_cleanup_after_private_verification,
            ), patch.object(
                render_skills,
                "_clear_directory_descriptor",
                wraps=render_skills._clear_directory_descriptor,
            ) as clear_directory, patch.object(
                render_skills.os,
                "unlink",
                wraps=real_unlink,
            ) as unlink, self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "private render cleanup root changed before recursive cleanup",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertIsNotNone(replacement_marker)
            assert replacement_marker is not None
            clear_directory.assert_not_called()
            unlink.assert_not_called()
            self.assertEqual(
                accepted_snapshot,
                self.snapshot(backup),
            )
            self.assertEqual([], list(displaced_cleanup.iterdir()))
            self.assertEqual(
                "foreign replacement cleanup bytes\n",
                replacement_marker.read_text(encoding="utf-8"),
            )

    def test_private_cleanup_root_replacement_before_removal_is_preserved(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-cleanup-root-race"
            backup.mkdir()
            (backup / "owned.txt").write_text(
                "accepted generated bytes\n",
                encoding="utf-8",
            )
            metadata = backup.stat()
            expected_entries = render_skills._capture_tree_entries(backup)
            real_rename = render_skills._atomic_rename_at_no_replace
            accepted_name: str | None = None
            replacement_identity: tuple[int, int] | None = None

            def replace_cleanup_root_before_private_move(
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> None:
                nonlocal accepted_name, replacement_identity
                if (
                    source_name.startswith(".render-cleanup-")
                    and replacement_identity is None
                ):
                    accepted_name = f"accepted-{source_name}"
                    os.rename(
                        source_name,
                        accepted_name,
                        src_dir_fd=source_descriptor,
                        dst_dir_fd=source_descriptor,
                    )
                    os.mkdir(
                        source_name,
                        0o700,
                        dir_fd=source_descriptor,
                    )
                    replacement = os.stat(
                        source_name,
                        dir_fd=source_descriptor,
                        follow_symlinks=False,
                    )
                    replacement_identity = (
                        replacement.st_dev,
                        replacement.st_ino,
                    )
                real_rename(
                    source_descriptor,
                    source_name,
                    destination_descriptor,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_atomic_rename_at_no_replace",
                side_effect=replace_cleanup_root_before_private_move,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "changed while moving into private cleanup quarantine.*preserved",
            ):
                render_skills._remove_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertIsNotNone(accepted_name)
            self.assertIsNotNone(replacement_identity)
            assert accepted_name is not None
            accepted = backup / accepted_name
            replacement_path = backup / accepted_name.removeprefix("accepted-")
            self.assertTrue(accepted.is_dir())
            self.assertTrue(replacement_path.is_dir())
            replacement = replacement_path.stat()
            self.assertEqual(
                replacement_identity,
                (replacement.st_dev, replacement.st_ino),
            )

    def test_swap_failure_does_not_overwrite_concurrent_output(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_replace = render_skills._atomic_rename_no_replace
            replace_count = 0

            def fail_install_after_concurrent_output(
                source: Path,
                destination: Path,
            ) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count == 2:
                    target.mkdir()
                    (target / "valuable.txt").write_text(
                        "preserve concurrent output\n",
                        encoding="utf-8",
                    )
                    raise OSError("forced install failure")
                real_replace(source, destination)

            with patch.object(
                render_skills,
                "_atomic_rename_no_replace",
                side_effect=fail_install_after_concurrent_output,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "output path gained concurrent state",
            ):
                render_skills.render_all(output_root=target)

            self.assertEqual(
                "preserve concurrent output\n",
                (target / "valuable.txt").read_text(encoding="utf-8"),
            )
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(prior, self.snapshot(backups[0]))

    def test_install_no_replace_preserves_last_moment_output(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_rename = render_skills._atomic_rename_no_replace
            rename_count = 0

            def create_output_immediately_before_install(
                source: Path,
                destination: Path,
            ) -> None:
                nonlocal rename_count
                rename_count += 1
                if rename_count == 2:
                    target.mkdir()
                    (target / "valuable.txt").write_text(
                        "last-moment output\n",
                        encoding="utf-8",
                    )
                real_rename(source, destination)

            with patch.object(
                render_skills,
                "_atomic_rename_no_replace",
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
            real_rename = render_skills._atomic_rename_no_replace
            rename_count = 0

            def fail_install_then_race_rollback(
                source: Path,
                destination: Path,
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
                real_rename(source, destination)

            with patch.object(
                render_skills,
                "_atomic_rename_no_replace",
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

    def test_non_directory_top_level_skill_root_is_rejected_without_mutation(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            displaced_skill = parent / "prior-stata-core"
            skill_root = target / "stata-core"
            skill_root.rename(displaced_skill)
            skill_root.write_text("not a skill directory\n", encoding="utf-8")
            displaced_before = self.snapshot(displaced_skill)

            with self.assertRaisesRegex(
                ValueError,
                "top-level skill root must be an ordinary directory",
            ):
                render_skills.render_all(output_root=target)

            self.assertEqual(
                "not a skill directory\n",
                skill_root.read_text(encoding="utf-8"),
            )
            self.assertEqual(displaced_before, self.snapshot(displaced_skill))
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_target_replacement_during_staging_is_rejected_and_preserved(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            accepted = parent / "accepted-tree"
            concurrent = parent / "concurrent-tree"
            concurrent.mkdir()
            sentinel = concurrent / "unrelated.md"
            sentinel.write_text("concurrent bytes\n", encoding="utf-8")
            before = self.snapshot(target)
            real_validate = render_skills.validate_rendered_tree

            def replace_target_after_staged_validation(*args: object) -> None:
                real_validate(*args)
                target.rename(accepted)
                target.symlink_to(concurrent, target_is_directory=True)

            with patch.object(
                render_skills,
                "validate_rendered_tree",
                side_effect=replace_target_after_staged_validation,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "changed after preflight",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(target.is_symlink())
            self.assertEqual(
                "concurrent bytes\n",
                sentinel.read_text(encoding="utf-8"),
            )
            self.assertEqual(before, self.snapshot(accepted))
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
            real_replace = render_skills._atomic_rename_no_replace
            replaced = False

            def substitute_immediately_before_backup(
                source: Path,
                destination: Path,
            ) -> None:
                nonlocal replaced
                if (
                    not replaced
                    and Path(source).resolve(strict=False)
                    == target.resolve(strict=False)
                ):
                    replaced = True
                    target.rename(accepted)
                    concurrent.rename(target)
                real_replace(source, destination)

            with patch.object(
                render_skills,
                "_atomic_rename_no_replace",
                side_effect=substitute_immediately_before_backup,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "moved from the render output path",
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
            dir=REPO_ROOT,
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
            dir=REPO_ROOT,
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

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "Linux case-sensitive behavior",
    )
    def test_case_distinct_linux_renderer_paths_remain_distinct(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            source = parent / "Source"
            output_parent = parent / "source"
            source.mkdir()
            output_parent.mkdir()
            if os.path.samefile(source, output_parent):
                self.skipTest("test filesystem is not case-sensitive")

            config = parent / "config.yaml"
            config.write_text("{}\n", encoding="utf-8")
            templates = parent / "templates"
            locks = parent / "locks"
            templates.mkdir()
            locks.mkdir()
            with patch.multiple(
                render_skills,
                TEMPLATES_ROOT=templates,
                LOCK_ROOT=locks,
            ):
                render_skills._validate_renderer_source_separation(
                    output_parent / "generated",
                    source,
                    config,
                )

            repository = parent / "repo"
            canonical_output = repository / "build" / "generated"
            tracked_file = (
                repository / "Build" / "generated" / "stata-core" / "SKILL.md"
            )
            canonical_output.mkdir(parents=True)
            tracked_file.parent.mkdir(parents=True)
            tracked_file.write_text("# Distinct tracked file\n", encoding="utf-8")
            with patch.multiple(
                render_skills,
                REPO_ROOT=repository,
                BUILD_ROOT=canonical_output,
            ), patch.object(
                render_skills,
                "tracked_source_paths",
                return_value=(Path("Build/generated/stata-core/SKILL.md"),),
            ):
                render_skills._assert_no_tracked_canonical_output_paths(
                    canonical_output
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

    def test_new_force_tracked_path_before_swap_preserves_canonical_output(
        self,
    ) -> None:
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
            marker.write_text("# Accepted modified tree\n", encoding="utf-8")
            before = self.snapshot(build_root)
            with patch.multiple(
                render_skills,
                REPO_ROOT=repository,
                BUILD_ROOT=build_root,
            ), patch.object(
                render_skills,
                "tracked_source_paths",
                side_effect=((), (tracked,)),
            ), self.assertRaisesRegex(
                ValueError,
                "contains Git-tracked paths",
            ):
                render_skills.render_all(output_root=build_root)

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

    def test_case_insensitive_repository_alias_cannot_bypass_output_guard(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            repository = parent / "Repository"
            repository.mkdir()
            repository_alias = parent / "rEPOSITORY"
            if (
                not repository_alias.exists()
                or not os.path.samefile(repository, repository_alias)
            ):
                self.skipTest("test filesystem is case-sensitive")
            build_root = repository / "build" / "generated"
            target = repository_alias / "custom-generated"

            with patch.object(
                render_skills,
                "REPO_ROOT",
                repository,
            ), patch.object(
                render_skills,
                "BUILD_ROOT",
                build_root,
            ):
                self.assertEqual(
                    build_root.resolve(strict=False),
                    render_skills._resolved_output_root(build_root),
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "in-repository render output must be build/generated",
                ):
                    render_skills._resolved_output_root(target)

            self.assertFalse(target.exists())

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "Linux case-sensitive behavior",
    )
    def test_case_distinct_linux_output_root_remains_allowed(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            repository = parent / "Repository"
            repository.mkdir()
            build_root = repository / "build" / "generated"
            target = parent / "rEPOSITORY" / "custom-generated"
            if target.parent.exists():
                self.skipTest("test filesystem is not case-sensitive")

            with patch.object(
                render_skills,
                "REPO_ROOT",
                repository,
            ), patch.object(
                render_skills,
                "BUILD_ROOT",
                build_root,
            ):
                self.assertEqual(
                    target.resolve(strict=False),
                    render_skills._resolved_output_root(target),
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

    def test_explicit_output_roots_remain_byte_identical(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            first = parent / "first"
            second = parent / "second"

            render_skills.render_all(output_root=first)
            render_skills.render_all(output_root=second)

            self.assertEqual(self.snapshot(first), self.snapshot(second))
            self.assertEqual([], self.transaction_artifacts(parent, first.name))
            self.assertEqual([], self.transaction_artifacts(parent, second.name))


if __name__ == "__main__":
    unittest.main()
