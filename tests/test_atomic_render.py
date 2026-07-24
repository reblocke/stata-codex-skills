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

            with patch.object(
                render_skills,
                "_render_tree",
                side_effect=fail_after_partial_render,
            ):
                with self.assertRaisesRegex(RuntimeError, "forced render failure"):
                    render_skills.render_all(output_root=target)

            self.assertEqual(before, self.snapshot(target))
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

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

            with patch.object(
                render_skills,
                "_render_tree",
                side_effect=render_incomplete_tree,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "staged render validation failed.*stata-core/SKILL.md",
                ):
                    render_skills.render_all(output_root=target)

            self.assertEqual(before, self.snapshot(target))
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

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
