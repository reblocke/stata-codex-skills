from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
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

    def test_success_replaces_complete_tree_and_cleans_transaction_artifacts(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = parent / "generated"
            target.mkdir()
            (target / "stale.txt").write_text("stale", encoding="utf-8")

            render_skills.render_all(output_root=target)

            self.assertFalse((target / "stale.txt").exists())
            self.assertEqual(
                {"stata-core", "stata-packages", "stata-c-plugins"},
                {path.name for path in target.iterdir() if path.is_dir()},
            )
            self.assertEqual([], self.transaction_artifacts(parent, target.name))

    def test_render_failure_preserves_previous_tree(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = parent / "generated"
            target.mkdir()
            (target / "sentinel.txt").write_text("prior", encoding="utf-8")
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

    def test_validation_failure_preserves_previous_tree(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = parent / "generated"
            target.mkdir()
            (target / "sentinel.txt").write_text("prior", encoding="utf-8")
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
            target = parent / "generated"
            target.mkdir()
            (target / "sentinel.txt").write_text("prior", encoding="utf-8")
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
            target = parent / "generated"
            target.mkdir()
            (target / "sentinel.txt").write_text("prior", encoding="utf-8")
            before = self.snapshot(target)
            real_replace = os.replace
            replace_count = 0

            def fail_new_tree_swap(source: Path, destination: Path) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count == 2:
                    raise OSError("forced swap failure")
                real_replace(source, destination)

            with patch.object(
                render_skills.os,
                "replace",
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
            target = parent / "generated"
            target.mkdir()
            (target / "sentinel.txt").write_text("prior", encoding="utf-8")
            before = self.snapshot(target)
            real_replace = os.replace
            replace_count = 0

            def fail_swap_and_restore(source: Path, destination: Path) -> None:
                nonlocal replace_count
                replace_count += 1
                if replace_count >= 2:
                    raise OSError(f"forced replace failure {replace_count}")
                real_replace(source, destination)

            with patch.object(
                render_skills.os,
                "replace",
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
            target = parent / "generated"
            target.mkdir()
            (target / "sentinel.txt").write_text("prior", encoding="utf-8")
            real_remove = render_skills._remove_path

            def fail_backup_cleanup(path: Path) -> None:
                if ".backup-" in path.name:
                    raise PermissionError("forced backup cleanup failure")
                real_remove(path)

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_remove_path",
                side_effect=fail_backup_cleanup,
            ), redirect_stdout(output):
                render_skills.render_all(output_root=target)

            self.assertFalse((target / "sentinel.txt").exists())
            self.assertIn("rendered tree was committed", output.getvalue())
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(
                {"sentinel.txt": b"prior"},
                self.snapshot(backups[0]),
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
