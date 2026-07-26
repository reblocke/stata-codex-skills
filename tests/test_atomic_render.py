from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import re
import shutil
import stat
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

    def test_clean_success_does_not_label_output_as_retained_stage(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = parent / "generated"
            output = io.StringIO()

            with redirect_stdout(output):
                render_skills.render_all(output_root=target)

            rendered_output = output.getvalue()
            self.assertTrue(target.is_dir())
            self.assertEqual(
                [],
                self.transaction_artifacts(parent, target.name),
            )
            self.assertNotIn("retained stage", rendered_output.lower())
            self.assertNotIn(
                "staged render tree retained for explicit cleanup",
                rendered_output.lower(),
            )
            self.assertNotRegex(
                rendered_output,
                (
                    r"(?i)(?:retained stage location|explicit cleanup at): "
                    + re.escape(str(target.resolve()))
                ),
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

    def test_changed_parent_before_materialization_is_not_accepted(
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
            real_materialize = render_skills._materialize_render_parent
            changed = False

            def change_parent_before_materialization(
                anchor: render_skills.RenderParentAnchor,
            ) -> render_skills.RenderParentHandle:
                nonlocal changed
                anchor.parent_path.rename(displaced_parent)
                anchor.parent_path.mkdir()
                replacement_sentinel.write_text(
                    "preserve replacement parent\n",
                    encoding="utf-8",
                )
                changed = True
                return real_materialize(anchor)

            with patch.object(
                render_skills,
                "_materialize_render_parent",
                side_effect=change_parent_before_materialization,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "output ancestor changed after destination validation",
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
            self.assertEqual(
                [],
                self.transaction_artifacts(approved_parent, target.name),
            )

    def test_new_render_parent_replacement_before_open_is_not_modified(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-parent-") as temp_root:
            root = Path(temp_root)
            anchor_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            root_metadata = os.fstat(anchor_descriptor)
            anchor = render_skills.RenderParentAnchor(
                parent_path=root / "new-parent" / "nested",
                ancestor_path=root,
                device=root_metadata.st_dev,
                inode=root_metadata.st_ino,
                descriptor=anchor_descriptor,
                missing_components=("new-parent", "nested"),
            )
            accepted_created = root / "accepted-created-parent"
            replacement = root / "new-parent"
            replacement_sentinel = replacement / "valuable.txt"
            replacement_mode: int | None = None
            changed = False
            real_open = render_skills._open_directory_at

            def replace_new_parent_before_open(
                parent_descriptor: int,
                name: str,
                display_path: Path,
            ) -> tuple[int, os.stat_result]:
                nonlocal changed, replacement_mode
                if name == "new-parent" and not changed:
                    replacement.rename(accepted_created)
                    replacement.mkdir(mode=0o700)
                    replacement_sentinel.write_text(
                        "preserve replacement parent\n",
                        encoding="utf-8",
                    )
                    replacement_mode = stat.S_IMODE(
                        replacement.stat().st_mode
                    )
                    changed = True
                return real_open(
                    parent_descriptor,
                    name,
                    display_path,
                )

            try:
                with patch.object(
                    render_skills,
                    "_open_directory_at",
                    side_effect=replace_new_parent_before_open,
                ), self.assertRaisesRegex(
                    render_skills.RenderTransactionError,
                    "new render output parent changed before first use",
                ):
                    render_skills._materialize_render_parent(anchor)
            finally:
                os.close(anchor_descriptor)

            self.assertTrue(changed)
            self.assertIsNotNone(replacement_mode)
            self.assertEqual(
                replacement_mode,
                stat.S_IMODE(replacement.stat().st_mode),
            )
            self.assertEqual(
                {"valuable.txt"},
                {path.name for path in replacement.iterdir()},
            )
            self.assertEqual(
                "preserve replacement parent\n",
                replacement_sentinel.read_text(encoding="utf-8"),
            )
            self.assertFalse((replacement / "nested").exists())
            self.assertTrue(accepted_created.is_dir())

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

    def test_intermediate_stage_replacement_is_not_modified(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_write = render_skills._write_staged_text
            writes = 0
            replacement_root: Path | None = None
            replacement_sentinel: Path | None = None

            def replace_skill_directory_after_first_write(
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
                nonlocal writes, replacement_root, replacement_sentinel
                real_write(
                    parent_handle,
                    staged_root,
                    staged_identity,
                    directory_identities,
                    relative_path,
                    text,
                )
                writes += 1
                if writes == 1:
                    skill_root = staged_root / "stata-core"
                    skill_root.rename(staged_root / "accepted-stata-core")
                    skill_root.mkdir(mode=0o700)
                    replacement_root = skill_root
                    replacement_sentinel = skill_root / "valuable.txt"
                    replacement_sentinel.write_text(
                        "preserve replacement directory\n",
                        encoding="utf-8",
                    )

            with patch.object(
                render_skills,
                "_write_staged_text",
                side_effect=replace_skill_directory_after_first_write,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "staging directory identity changed before writing",
            ):
                render_skills.render_all(output_root=target)

            self.assertGreaterEqual(writes, 1)
            self.assertEqual(prior, self.snapshot(target))
            self.assertIsNotNone(replacement_root)
            self.assertIsNotNone(replacement_sentinel)
            assert replacement_root is not None
            assert replacement_sentinel is not None
            self.assertEqual(
                0o700,
                stat.S_IMODE(replacement_root.stat().st_mode),
            )
            self.assertEqual(
                "preserve replacement directory\n",
                replacement_sentinel.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                {"valuable.txt"},
                {path.name for path in replacement_root.iterdir()},
            )

    def test_new_stage_directory_replacement_before_open_is_not_modified(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_open = render_skills._open_directory_at
            changed = False
            replacement_root: Path | None = None
            replacement_sentinel: Path | None = None

            def replace_new_directory_before_open(
                parent_descriptor: int,
                name: str,
                display_path: Path,
            ) -> tuple[int, os.stat_result]:
                nonlocal changed, replacement_root, replacement_sentinel
                if (
                    not changed
                    and name == "stata-core"
                    and display_path.parent.name.startswith(
                        ".generated.stage-"
                    )
                ):
                    display_path.rename(
                        display_path.parent / "accepted-stata-core"
                    )
                    display_path.mkdir(mode=0o700)
                    replacement_root = display_path
                    replacement_sentinel = display_path / "valuable.txt"
                    replacement_sentinel.write_text(
                        "preserve first-open replacement\n",
                        encoding="utf-8",
                    )
                    changed = True
                return real_open(
                    parent_descriptor,
                    name,
                    display_path,
                )

            with patch.object(
                render_skills,
                "_open_directory_at",
                side_effect=replace_new_directory_before_open,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "new staging directory changed before first use",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(changed)
            self.assertEqual(prior, self.snapshot(target))
            self.assertIsNotNone(replacement_root)
            self.assertIsNotNone(replacement_sentinel)
            assert replacement_root is not None
            assert replacement_sentinel is not None
            self.assertEqual(
                0o700,
                stat.S_IMODE(replacement_root.stat().st_mode),
            )
            self.assertEqual(
                "preserve first-open replacement\n",
                replacement_sentinel.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                {"valuable.txt"},
                {path.name for path in replacement_root.iterdir()},
            )

    def test_primary_render_failure_survives_retention_interruption(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            primary = RuntimeError("primary replacement failure")
            retention = KeyboardInterrupt("retention interrupted")
            output = io.StringIO()

            with patch.object(
                render_skills,
                "_replace_rendered_tree",
                side_effect=primary,
            ), patch.object(
                render_skills,
                "_retain_owned_directory",
                side_effect=retention,
            ), redirect_stdout(output), self.assertRaises(
                RuntimeError
            ) as raised:
                render_skills.render_all(output_root=target)

            self.assertIs(primary, raised.exception)
            self.assertEqual(prior, self.snapshot(target))
            stages = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".stage-" in path.name
            ]
            self.assertEqual(1, len(stages))
            self.assertIn(
                "staged render retention also encountered KeyboardInterrupt",
                output.getvalue(),
            )

    def test_retention_interruption_without_primary_closes_descriptors(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            retention = KeyboardInterrupt("retention interrupted")
            before_fds = len(os.listdir("/dev/fd"))

            with patch.object(
                render_skills,
                "_replace_rendered_tree",
                return_value=None,
            ), patch.object(
                render_skills,
                "_retain_staged_root",
                side_effect=retention,
            ), self.assertRaises(KeyboardInterrupt) as raised:
                render_skills.render_all(output_root=target)

            self.assertIs(retention, raised.exception)
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))
            self.assertEqual(prior, self.snapshot(target))

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

    def test_substituted_staged_tree_after_validation_is_not_committed(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            validated_stage = parent / "validated-stage"
            real_replace = render_skills._replace_rendered_tree

            def substitute_before_transaction(
                parent_handle: render_skills.RenderParentHandle,
                staged_root: Path,
                output_root: Path,
                expected_output: render_skills.RenderOutputState,
                expected_staged: render_skills.RenderOutputState,
                validator: object,
                verify_inputs: object,
            ) -> None:
                staged_root.rename(validated_stage)
                shutil.copytree(validated_stage, staged_root)
                (staged_root / "stata-core" / "SKILL.md").write_text(
                    "# Unvalidated replacement\n",
                    encoding="utf-8",
                )
                real_replace(
                    parent_handle,
                    staged_root,
                    output_root,
                    expected_output,
                    expected_staged,
                    validator,
                    verify_inputs,
                )

            output = io.StringIO()
            with patch.object(
                render_skills,
                "_replace_rendered_tree",
                side_effect=substitute_before_transaction,
            ), redirect_stdout(output), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "no longer matches its accepted identity",
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
                _parent_handle: render_skills.RenderParentHandle,
                staged_root: Path,
                _output_root: Path,
                _expected_output: render_skills.RenderOutputState,
                _expected_staged: render_skills.RenderOutputState,
                _validator: object,
                _verify_inputs: object,
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

    def test_structural_validation_uses_retained_stage_inventory(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            displaced_parent = parent.parent / f"{parent.name}-displaced"
            real_render = render_skills._render_tree
            real_validate = render_skills.validate_rendered_state
            alternate_tree_was_present = False

            def render_missing_required_file(
                output_root: Path,
                *args: object,
            ) -> None:
                real_render(output_root, *args)
                (output_root / "stata-core" / "SKILL.md").unlink()

            def change_public_parent_during_validation(
                state: render_skills.RenderOutputState,
                *args: object,
            ) -> None:
                nonlocal alternate_tree_was_present
                stage = next(
                    path
                    for path in self.transaction_artifacts(
                        parent,
                        target.name,
                    )
                    if ".stage-" in path.name
                )
                stage_name = stage.name
                parent.rename(displaced_parent)
                parent.mkdir()
                shutil.copytree(
                    displaced_parent / target.name,
                    parent / stage_name,
                )
                alternate_tree_was_present = True
                try:
                    real_validate(state, *args)
                finally:
                    shutil.rmtree(parent)
                    displaced_parent.rename(parent)

            with patch.object(
                render_skills,
                "_render_tree",
                side_effect=render_missing_required_file,
            ), patch.object(
                render_skills,
                "validate_rendered_state",
                side_effect=change_public_parent_during_validation,
            ), self.assertRaisesRegex(
                ValueError,
                "missing files: stata-core/SKILL.md",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(alternate_tree_was_present)
            self.assertEqual(prior, self.snapshot(target))
            self.assert_single_retained_stage(parent, target.name)

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

    def test_interrupt_with_concurrent_output_preserves_backup_and_reports_recovery(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_verify = render_skills._verify_output_state_at
            interrupted = False

            def interrupt_after_concurrent_output(
                parent_handle: render_skills.RenderParentHandle,
                name: str,
                backup: Path,
                expected: render_skills.RenderOutputState,
            ) -> None:
                nonlocal interrupted
                if ".backup-" in name and not interrupted:
                    interrupted = True
                    target.mkdir()
                    (target / "valuable.txt").write_text(
                        "preserve concurrent output\n",
                        encoding="utf-8",
                    )
                    raise KeyboardInterrupt(
                        "forced interrupt with concurrent output"
                    )
                real_verify(parent_handle, name, backup, expected)

            with patch.object(
                render_skills,
                "_verify_output_state_at",
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

    def test_failed_restore_reports_moved_prior_tree_location(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            before = self.snapshot(target)
            real_rename = render_skills._rename_render_entry
            moved_prior = parent / ".relocated-prior-tree"
            moved = False

            def fail_install_and_move_before_restore(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
            ) -> None:
                nonlocal moved
                if ".stage-" in source_name and destination_name == target.name:
                    raise OSError("forced staged install failure")
                if (
                    ".backup-" in source_name
                    and destination_name == target.name
                ):
                    os.rename(
                        source_name,
                        moved_prior.name,
                        src_dir_fd=parent_handle.descriptor,
                        dst_dir_fd=parent_handle.descriptor,
                    )
                    moved = True
                real_rename(
                    parent_handle,
                    source_name,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=fail_install_and_move_before_restore,
            ), self.assertRaises(
                render_skills.RenderTransactionError
            ) as raised:
                render_skills.render_all(output_root=target)

            self.assertTrue(moved)
            self.assertFalse(target.exists())
            self.assertTrue(moved_prior.is_dir())
            self.assertEqual(before, self.snapshot(moved_prior))
            message = str(raised.exception)
            self.assertIn(
                f"retained prior-tree location: {moved_prior.resolve()}",
                message,
            )
            self.assertNotIn("the prior tree remains at", message)

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

    def test_default_cleanup_retains_late_replacement_without_unlinking(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            backup = parent / ".generated.backup-retained"
            backup.mkdir()
            owned = backup / "owned.txt"
            owned.write_text(
                "accepted generated bytes\n",
                encoding="utf-8",
            )
            metadata = backup.stat()
            expected_entries = render_skills._capture_tree_entries(backup)
            real_verify = render_skills._verify_directory_descriptor_tree
            verification_count = 0
            changed = False

            def add_late_replacement_after_first_verification(
                descriptor: int,
                display_path: Path,
                expected: dict[str, render_skills.RenderTreeEntry],
                relative_parts: tuple[str, ...] = (),
            ) -> None:
                nonlocal verification_count, changed
                real_verify(
                    descriptor,
                    display_path,
                    expected,
                    relative_parts,
                )
                if not relative_parts:
                    verification_count += 1
                    if verification_count == 1:
                        os.rename(
                            "owned.txt",
                            "accepted.txt",
                            src_dir_fd=descriptor,
                            dst_dir_fd=descriptor,
                        )
                        replacement = os.open(
                            "owned.txt",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=descriptor,
                        )
                        try:
                            os.write(
                                replacement,
                                b"late replacement bytes\n",
                            )
                        finally:
                            os.close(replacement)
                        changed = True

            with patch.object(
                render_skills,
                "_verify_directory_descriptor_tree",
                side_effect=add_late_replacement_after_first_verification,
            ), patch.object(
                render_skills.os,
                "unlink",
                side_effect=AssertionError("automatic unlink is forbidden"),
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "changed during private pre-delete verification",
            ):
                render_skills._retain_owned_directory(
                    backup,
                    metadata.st_dev,
                    metadata.st_ino,
                    expected_entries,
                )

            self.assertTrue(changed)
            self.assertEqual(
                "accepted generated bytes\n",
                (backup / "accepted.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "late replacement bytes\n",
                (backup / "owned.txt").read_text(encoding="utf-8"),
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
            real_replace = render_skills._rename_render_entry
            replace_count = 0

            def fail_install_after_concurrent_output(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
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
                real_replace(
                    parent_handle,
                    source_name,
                    destination_name,
                )

            with patch.object(
                render_skills,
                "_rename_render_entry",
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
            real_validate = render_skills.validate_rendered_state

            def replace_target_after_staged_validation(*args: object) -> None:
                real_validate(*args)
                target.rename(accepted)
                target.symlink_to(concurrent, target_is_directory=True)

            with patch.object(
                render_skills,
                "validate_rendered_state",
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
            self.assert_single_retained_stage(parent, target.name)

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
            self.assert_single_retained_stage(
                build_root.parent,
                build_root.name,
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

    def test_shared_atomic_rename_unavailability_is_normalized(self) -> None:
        with patch.object(
            render_skills,
            "_shared_atomic_rename_at_no_replace",
            side_effect=RuntimeError("primitive unavailable"),
        ), self.assertRaisesRegex(
            render_skills.RenderTransactionError,
            "primitive unavailable",
        ):
            render_skills._atomic_rename_at_no_replace(1, "source", 2, "target")

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

    def test_input_change_inside_transaction_restores_prior_tree(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            content_root = parent / "content"
            shutil.copytree(REPO_ROOT / "content", content_root)
            changed_path = content_root / "core" / "panel-data.yaml"
            real_verify = render_skills.verify_render_inputs
            verify_count = 0
            changed = False

            def change_after_transaction_checkpoint(
                snapshot: render_skills.RenderInputSnapshot,
            ) -> None:
                nonlocal verify_count, changed
                verify_count += 1
                real_verify(snapshot)
                if verify_count == 3:
                    entry = yaml.safe_load(
                        changed_path.read_text(encoding="utf-8")
                    )
                    entry["title"] = "Changed during placement"
                    changed_path.write_text(
                        yaml.safe_dump(entry, sort_keys=False),
                        encoding="utf-8",
                    )
                    changed = True

            with patch.object(
                render_skills,
                "verify_render_inputs",
                side_effect=change_after_transaction_checkpoint,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "render inputs changed after validation",
            ):
                render_skills.render_all(
                    output_root=target,
                    content_root=content_root,
                )

            self.assertTrue(changed)
            self.assertGreaterEqual(verify_count, 4)
            self.assertEqual(prior, self.snapshot(target))
            self.assert_single_retained_stage(parent, target.name)

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

    def test_parent_change_before_success_cleanup_is_reported(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            root = Path(temp_root)
            approved_parent = root / "approved-parent"
            approved_parent.mkdir()
            target = self.seeded_target(approved_parent)
            displaced_parent = root / "displaced-parent"
            prior = self.snapshot(target)
            real_cleanup = render_skills._remove_verified_backup
            changed = False

            def change_parent_then_cleanup(
                *args: object,
                **kwargs: object,
            ) -> None:
                nonlocal changed
                approved_parent.rename(displaced_parent)
                approved_parent.mkdir()
                (approved_parent / "sentinel.txt").write_text(
                    "replacement parent bytes\n",
                    encoding="utf-8",
                )
                changed = True
                real_cleanup(*args, **kwargs)

            with patch.object(
                render_skills,
                "_remove_verified_backup",
                side_effect=change_parent_then_cleanup,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                f"survives at {re.escape(str(displaced_parent.resolve()))}",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(changed)
            self.assertEqual(
                "replacement parent bytes\n",
                (approved_parent / "sentinel.txt").read_text(encoding="utf-8"),
            )
            self.assertTrue((displaced_parent / target.name).is_dir())
            self.assertFalse(target.exists())
            backups = [
                path
                for path in displaced_parent.iterdir()
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(prior, self.snapshot(backups[0]))

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

    def test_prior_tree_mode_change_at_backup_is_preserved(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            changed_file = target / "stata-core" / "SKILL.md"
            real_rename = render_skills._rename_render_entry
            changed = False

            def change_mode_then_move(
                parent_handle: render_skills.RenderParentHandle,
                source_name: str,
                destination_name: str,
            ) -> None:
                nonlocal changed
                if source_name == target.name and ".backup-" in destination_name:
                    changed_file.chmod(0o600)
                    changed = True
                real_rename(parent_handle, source_name, destination_name)

            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=change_mode_then_move,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "prior tree could not be restored.*remains at",
            ):
                render_skills.render_all(output_root=target)

            self.assertTrue(changed)
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(
                0o600,
                stat.S_IMODE(
                    (backups[0] / "stata-core" / "SKILL.md").stat().st_mode
                ),
            )
            self.assertFalse(target.exists())

    def test_backup_root_mode_change_at_cleanup_is_preserved(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            real_remove = render_skills._remove_owned_directory
            changed_mode: int | None = None

            def change_root_mode_before_cleanup(
                directory: Path,
                expected_device: int,
                expected_inode: int,
                expected_entries: tuple[
                    render_skills.RenderTreeEntry, ...
                ],
                *,
                expected_mode: int | None = None,
                parent_descriptor: int | None = None,
            ) -> None:
                nonlocal changed_mode
                changed_mode = (
                    0o700 if expected_mode != 0o700 else 0o755
                )
                directory.chmod(changed_mode)
                real_remove(
                    directory,
                    expected_device,
                    expected_inode,
                    expected_entries,
                    expected_mode=expected_mode,
                    parent_descriptor=parent_descriptor,
                )

            with patch.object(
                render_skills,
                "_remove_owned_directory",
                side_effect=change_root_mode_before_cleanup,
            ), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "verified cleanup of the accepted prior tree failed",
            ):
                render_skills.render_all(output_root=target)

            self.assertIsNotNone(changed_mode)
            backups = [
                path
                for path in self.transaction_artifacts(parent, target.name)
                if ".backup-" in path.name
            ]
            self.assertEqual(1, len(backups))
            self.assertEqual(prior, self.snapshot(backups[0]))
            self.assertEqual(
                changed_mode,
                stat.S_IMODE(backups[0].stat().st_mode),
            )

    def test_moved_rejected_tree_is_rescanned_for_final_report(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = parent / "generated"
            relocated_recovery = parent / ".relocated-rejected-tree"
            original_recovery: Path | None = None
            real_verify = render_skills._verify_output_state_at
            real_preserve = render_skills._preserve_installed_entry
            forced_failure = False

            def fail_first_post_placement_verification(
                parent_handle: render_skills.RenderParentHandle,
                name: str,
                display_path: Path,
                expected: render_skills.RenderOutputState,
            ) -> None:
                nonlocal forced_failure
                real_verify(
                    parent_handle,
                    name,
                    display_path,
                    expected,
                )
                if name == target.name and not forced_failure:
                    forced_failure = True
                    raise render_skills.RenderTransactionError(
                        "forced post-placement verification failure"
                    )

            def preserve_then_relocate(
                parent_handle: render_skills.RenderParentHandle,
                output_root: Path,
                installed_identity: tuple[int, int],
            ) -> Path:
                nonlocal original_recovery
                original_recovery = real_preserve(
                    parent_handle,
                    output_root,
                    installed_identity,
                )
                original_recovery.rename(relocated_recovery)
                return original_recovery

            with patch.object(
                render_skills,
                "_verify_output_state_at",
                side_effect=fail_first_post_placement_verification,
            ), patch.object(
                render_skills,
                "_preserve_installed_entry",
                side_effect=preserve_then_relocate,
            ), self.assertRaises(
                render_skills.RenderTransactionError,
            ) as raised:
                render_skills.render_all(output_root=target)

            self.assertTrue(forced_failure)
            self.assertIsNotNone(original_recovery)
            assert original_recovery is not None
            self.assertFalse(original_recovery.exists())
            self.assertTrue(relocated_recovery.is_dir())
            self.assertIn(
                str(relocated_recovery.resolve()),
                str(raised.exception),
            )
            self.assertNotIn(
                str(original_recovery),
                str(raised.exception),
            )

    def test_moved_backup_is_rescanned_for_cleanup_failure(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent = Path(temp_root)
            target = self.seeded_target(parent)
            prior = self.snapshot(target)
            relocated_backup = parent / ".relocated-prior-tree"
            original_backup: Path | None = None
            real_remove = render_skills._remove_verified_backup

            def relocate_then_remove(
                backup_root: Path,
                expected: render_skills.RenderOutputState,
                parent_handle: render_skills.RenderParentHandle | None = None,
            ) -> None:
                nonlocal original_backup
                original_backup = backup_root
                original_backup.rename(relocated_backup)
                real_remove(
                    backup_root,
                    expected,
                    parent_handle,
                )

            with patch.object(
                render_skills,
                "_remove_verified_backup",
                side_effect=relocate_then_remove,
            ), self.assertRaises(
                render_skills.RenderTransactionError,
            ) as raised:
                render_skills.render_all(output_root=target)

            self.assertIsNotNone(original_backup)
            assert original_backup is not None
            self.assertFalse(original_backup.exists())
            self.assertTrue(relocated_backup.is_dir())
            self.assertEqual(prior, self.snapshot(relocated_backup))
            self.assertIn(
                str(relocated_backup.resolve()),
                str(raised.exception),
            )
            self.assertNotIn(str(original_backup), str(raised.exception))

    def test_staged_write_close_interruption_preserves_primary_and_closes_all(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-close-") as temp_root:
            parent_path = Path(temp_root)
            staged_root = parent_path / ".generated.stage-close"
            staged_root.mkdir(mode=0o755)
            staged_root.chmod(0o755)
            parent_descriptor = os.open(
                parent_path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            parent_metadata = os.fstat(parent_descriptor)
            staged_metadata = staged_root.stat()
            parent = render_skills.RenderParentHandle(
                path=parent_path,
                device=parent_metadata.st_dev,
                inode=parent_metadata.st_ino,
                descriptor=parent_descriptor,
            )
            primary = RuntimeError("forced staged write failure")
            real_close = os.close
            close_calls: list[int] = []

            def close_then_interrupt_first(descriptor: int) -> None:
                close_calls.append(descriptor)
                real_close(descriptor)
                if len(close_calls) == 1:
                    raise KeyboardInterrupt(
                        "forced descriptor finalization interruption"
                    )

            try:
                with patch.object(
                    render_skills.os,
                    "write",
                    side_effect=primary,
                ), patch.object(
                    render_skills.os,
                    "close",
                    side_effect=close_then_interrupt_first,
                ), self.assertRaises(RuntimeError) as raised:
                    render_skills._write_staged_text(
                        parent,
                        staged_root,
                        (
                            staged_metadata.st_dev,
                            staged_metadata.st_ino,
                        ),
                        {
                            (): (
                                staged_metadata.st_dev,
                                staged_metadata.st_ino,
                            )
                        },
                        Path("skill") / "SKILL.md",
                        "# rendered\n",
                    )
            finally:
                real_close(parent_descriptor)

            self.assertIs(primary, raised.exception)
            self.assertEqual(3, len(close_calls))
            self.assertEqual(3, len(set(close_calls)))
            for descriptor in close_calls:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_open_directory_close_interruption_preserves_fstat_failure(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="atomic-render-open-") as temp_root:
            parent = Path(temp_root)
            child = parent / "child"
            child.mkdir()
            parent_descriptor = os.open(
                parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            primary = RuntimeError("forced directory fstat failure")
            real_close = os.close
            closed_descriptors: list[int] = []

            def close_then_interrupt(descriptor: int) -> None:
                closed_descriptors.append(descriptor)
                real_close(descriptor)
                raise KeyboardInterrupt(
                    "forced directory close interruption"
                )

            try:
                with patch.object(
                    render_skills.os,
                    "fstat",
                    side_effect=primary,
                ), patch.object(
                    render_skills.os,
                    "close",
                    side_effect=close_then_interrupt,
                ), self.assertRaises(RuntimeError) as raised:
                    render_skills._open_directory_at(
                        parent_descriptor,
                        child.name,
                        child,
                    )
            finally:
                real_close(parent_descriptor)

            self.assertIs(primary, raised.exception)
            self.assertEqual(1, len(closed_descriptors))
            with self.assertRaises(OSError):
                os.fstat(closed_descriptors[0])

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and bool(getattr(os, "O_NONBLOCK", 0)),
        "nonblocking FIFO replacement requires POSIX",
    )
    def test_regular_file_helpers_reject_fifo_replacement_without_waiting(
        self,
    ) -> None:
        cases = (
            (
                "input capture",
                lambda path, _metadata: (
                    render_skills._capture_render_input_file(path)
                ),
                ValueError,
            ),
            (
                "hash",
                lambda path, metadata: (
                    render_skills._sha256_regular_file_no_follow(
                        path,
                        expected_metadata=metadata,
                    )
                ),
                render_skills.RenderTransactionError,
            ),
            (
                "content read",
                lambda path, metadata: (
                    render_skills._read_regular_file_no_follow(
                        path,
                        expected_metadata=metadata,
                    )
                ),
                render_skills.RenderTransactionError,
            ),
        )
        for label, invoke, expected_exception in cases:
            with self.subTest(label=label), TemporaryDirectory(
                prefix="atomic-render-fifo-"
            ) as temp_root:
                root = Path(temp_root)
                source = root / "source.txt"
                accepted = root / "accepted-source.txt"
                source.write_text("accepted bytes\n", encoding="utf-8")
                expected_metadata = source.lstat()
                real_open = os.open
                observed_flags: list[int] = []

                def replace_before_open(
                    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
                    flags: int,
                    mode: int = 0o777,
                    *,
                    dir_fd: int | None = None,
                ) -> int:
                    observed_flags.append(flags)
                    source.rename(accepted)
                    os.mkfifo(source)
                    if not flags & os.O_NONBLOCK:
                        raise AssertionError(
                            "ordinary-file open omitted O_NONBLOCK"
                        )
                    return real_open(
                        path,
                        flags,
                        mode,
                        dir_fd=dir_fd,
                    )

                with patch.object(
                    render_skills.os,
                    "open",
                    side_effect=replace_before_open,
                ), self.assertRaises(expected_exception):
                    invoke(source, expected_metadata)

                self.assertEqual(1, len(observed_flags))
                self.assertTrue(observed_flags[0] & os.O_NONBLOCK)
                self.assertTrue(source.exists())
                self.assertTrue(stat.S_ISFIFO(source.lstat().st_mode))
                self.assertEqual(
                    "accepted bytes\n",
                    accepted.read_text(encoding="utf-8"),
                )

    def test_top_level_render_rename_is_durably_synchronized(self) -> None:
        with TemporaryDirectory(prefix="atomic-render-") as temp_root:
            parent_path = Path(temp_root)
            source = parent_path / "source"
            source.mkdir()
            parent_metadata = parent_path.stat()
            parent_descriptor = os.open(parent_path, os.O_RDONLY)
            parent = render_skills.RenderParentHandle(
                path=parent_path,
                device=parent_metadata.st_dev,
                inode=parent_metadata.st_ino,
                descriptor=parent_descriptor,
            )
            real_fsync = os.fsync
            synchronized: list[int] = []

            def record_fsync(descriptor: int) -> None:
                synchronized.append(descriptor)
                real_fsync(descriptor)

            try:
                with patch.object(
                    render_skills.os,
                    "fsync",
                    side_effect=record_fsync,
                ):
                    render_skills._rename_render_entry(
                        parent,
                        source.name,
                        "destination",
                    )
            finally:
                os.close(parent_descriptor)

            self.assertTrue((parent_path / "destination").is_dir())
            self.assertIn(parent_descriptor, synchronized)

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


class CallerWorkspacePreservationTests(unittest.TestCase):
    @staticmethod
    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_successful_private_workspace_is_removed(self) -> None:
        with TemporaryDirectory(prefix="caller-workspace-success-") as temp_root:
            parent = Path(temp_root)
            workspace = parent / "workspace"
            workspace.mkdir(mode=0o700)
            output = io.StringIO()

            with redirect_stdout(output), render_skills._retained_workspace_scope(
                workspace,
                "success",
            ):
                (workspace / "sentinel.txt").write_text(
                    "temporary bytes\n",
                    encoding="utf-8",
                )

            self.assertFalse(workspace.exists())
            self.assertEqual([], list(parent.glob(".render-cleanup-*")))
            self.assertNotIn("retained", output.getvalue().lower())

    def test_workspace_parent_change_before_cleanup_preserves_workspace(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="caller-workspace-parent-") as temp_root:
            root = Path(temp_root)
            approved_parent = root / "approved-parent"
            approved_parent.mkdir()
            workspace = approved_parent / "workspace"
            workspace.mkdir(mode=0o700)
            displaced_parent = root / "displaced-parent"
            real_capture = render_skills._capture_directory_descriptor_tree
            output = io.StringIO()

            def capture_then_change_parent(
                *args: object,
                **kwargs: object,
            ) -> tuple[render_skills.RenderTreeEntry, ...]:
                captured = real_capture(*args, **kwargs)
                approved_parent.rename(displaced_parent)
                approved_parent.mkdir()
                (approved_parent / "sentinel.txt").write_text(
                    "replacement parent bytes\n",
                    encoding="utf-8",
                )
                return captured

            with patch.object(
                render_skills,
                "_capture_directory_descriptor_tree",
                side_effect=capture_then_change_parent,
            ), redirect_stdout(output), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "render output parent changed",
            ):
                with render_skills._retained_workspace_scope(
                    workspace,
                    "success",
                ):
                    (workspace / "temporary.txt").write_text(
                        "temporary bytes\n",
                        encoding="utf-8",
                    )

            retained = displaced_parent / "workspace"
            self.assertTrue(retained.is_dir())
            self.assertEqual(
                "temporary bytes\n",
                (retained / "temporary.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "replacement parent bytes\n",
                (approved_parent / "sentinel.txt").read_text(encoding="utf-8"),
            )
            self.assertIn(
                (
                    "workspace retained for explicit cleanup at: "
                    f"{retained.resolve()}"
                ),
                output.getvalue(),
            )

    def test_workspace_parent_change_after_cleanup_cannot_report_success(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="caller-workspace-parent-") as temp_root:
            root = Path(temp_root)
            approved_parent = root / "approved-parent"
            approved_parent.mkdir()
            workspace = approved_parent / "workspace"
            workspace.mkdir(mode=0o700)
            displaced_parent = root / "displaced-parent"
            real_remove = render_skills._remove_owned_directory
            output = io.StringIO()

            def cleanup_then_change_parent(
                *args: object,
                **kwargs: object,
            ) -> None:
                real_remove(*args, **kwargs)
                approved_parent.rename(displaced_parent)
                approved_parent.mkdir()
                (approved_parent / "sentinel.txt").write_text(
                    "replacement parent bytes\n",
                    encoding="utf-8",
                )

            with patch.object(
                render_skills,
                "_remove_owned_directory",
                side_effect=cleanup_then_change_parent,
            ), redirect_stdout(output), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "render output parent changed",
            ):
                with render_skills._retained_workspace_scope(
                    workspace,
                    "success",
                ):
                    (workspace / "temporary.txt").write_text(
                        "temporary bytes\n",
                        encoding="utf-8",
                    )

            self.assertFalse((displaced_parent / "workspace").exists())
            self.assertEqual(
                "replacement parent bytes\n",
                (approved_parent / "sentinel.txt").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "workspace was verified and removed",
                output.getvalue(),
            )
            self.assertNotIn(
                "workspace retained for explicit cleanup",
                output.getvalue(),
            )

    def test_post_removal_workspace_error_reports_no_verified_survivor(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="caller-workspace-cleanup-") as temp_root:
            parent = Path(temp_root)
            workspace = parent / "workspace"
            workspace.mkdir(mode=0o700)
            real_remove = render_skills._remove_owned_directory
            output = io.StringIO()

            def remove_then_fail(
                *args: object,
                **kwargs: object,
            ) -> None:
                real_remove(*args, **kwargs)
                raise OSError("forced post-removal workspace failure")

            with patch.object(
                render_skills,
                "_remove_owned_directory",
                side_effect=remove_then_fail,
            ), redirect_stdout(output), self.assertRaisesRegex(
                OSError,
                "forced post-removal workspace failure",
            ):
                with render_skills._retained_workspace_scope(
                    workspace,
                    "success",
                ):
                    (workspace / "temporary.txt").write_text(
                        "temporary bytes\n",
                        encoding="utf-8",
                    )

            self.assertFalse(workspace.exists())
            self.assertIn(
                "no verified surviving workspace path was found",
                output.getvalue(),
            )
            self.assertNotIn(
                "workspace retained for explicit cleanup",
                output.getvalue(),
            )

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

    def assert_stage_initialization_interruption(
        self,
        failure_patch_factory,
        expected_error: BaseException,
        expected_mode: int,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="stage-initialization-test-")
        )
        parent_descriptor = os.open(
            fixture_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        parent_metadata = os.fstat(parent_descriptor)
        parent = render_skills.RenderParentHandle(
            path=fixture_root,
            device=parent_metadata.st_dev,
            inode=parent_metadata.st_ino,
            descriptor=parent_descriptor,
        )
        real_retained_location = render_skills._retained_child_location
        accepted_identities: list[tuple[int, int]] = []

        def record_retained_location(
            parent_handle: render_skills.RenderParentHandle,
            expected_identity: tuple[int, int] | None,
        ) -> str:
            self.assertIsNotNone(expected_identity)
            assert expected_identity is not None
            accepted_identities.append(expected_identity)
            return real_retained_location(parent_handle, expected_identity)

        output = io.StringIO()
        before_fds = len(os.listdir("/dev/fd"))
        try:
            with failure_patch_factory(parent_descriptor), patch.object(
                render_skills,
                "_retained_child_location",
                side_effect=record_retained_location,
            ), redirect_stdout(output), self.assertRaises(
                type(expected_error)
            ) as raised:
                render_skills._create_staged_root_at(parent, "generated")

            self.assertIs(expected_error, raised.exception)
            stages = sorted(fixture_root.glob(".generated.stage-*"))
            self.assertEqual(1, len(stages))
            stage = stages[0]
            stage_metadata = stage.lstat()
            self.assertEqual(
                [(stage_metadata.st_dev, stage_metadata.st_ino)],
                accepted_identities,
            )
            self.assertEqual(expected_mode, stage_metadata.st_mode & 0o7777)
            self.assertIn(
                f"retained stage location: {stage}",
                output.getvalue(),
            )
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))
        finally:
            os.close(parent_descriptor)
            shutil.rmtree(fixture_root)

    def test_stage_fchmod_interruption_retains_identity_and_primary(
        self,
    ) -> None:
        primary = KeyboardInterrupt("forced stage fchmod interruption")

        def failure_patch_factory(_parent_descriptor: int):
            return patch.object(
                render_skills.os,
                "fchmod",
                side_effect=primary,
            )

        self.assert_stage_initialization_interruption(
            failure_patch_factory,
            primary,
            0o500,
        )

    def test_stage_child_fsync_interruption_retains_identity_and_primary(
        self,
    ) -> None:
        primary = SystemExit("forced stage child-fsync interruption")
        real_fsync = os.fsync

        def failure_patch_factory(parent_descriptor: int):
            def interrupt_child_fsync(descriptor: int) -> None:
                if descriptor != parent_descriptor:
                    raise primary
                real_fsync(descriptor)

            return patch.object(
                render_skills.os,
                "fsync",
                side_effect=interrupt_child_fsync,
            )

        self.assert_stage_initialization_interruption(
            failure_patch_factory,
            primary,
            render_skills.CANONICAL_DIRECTORY_MODE,
        )

    def test_stage_parent_fsync_interruption_retains_identity_and_primary(
        self,
    ) -> None:
        primary = KeyboardInterrupt("forced stage parent-fsync interruption")
        real_fsync = os.fsync

        def failure_patch_factory(parent_descriptor: int):
            def interrupt_parent_fsync(descriptor: int) -> None:
                if descriptor == parent_descriptor:
                    raise primary
                real_fsync(descriptor)

            return patch.object(
                render_skills.os,
                "fsync",
                side_effect=interrupt_parent_fsync,
            )

        self.assert_stage_initialization_interruption(
            failure_patch_factory,
            primary,
            render_skills.CANONICAL_DIRECTORY_MODE,
        )

    def test_stage_success_close_interruption_reports_retained_identity(
        self,
    ) -> None:
        close_error = SystemExit("forced stage close interruption")
        real_close = os.close

        def failure_patch_factory(_parent_descriptor: int):
            def close_then_interrupt(descriptor: int) -> None:
                real_close(descriptor)
                raise close_error

            return patch.object(
                render_skills.os,
                "close",
                side_effect=close_then_interrupt,
            )

        self.assert_stage_initialization_interruption(
            failure_patch_factory,
            close_error,
            render_skills.CANONICAL_DIRECTORY_MODE,
        )

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

    def test_determinism_failure_retains_and_reports_outer_workspace(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="determinism-preservation-test-")
        )
        retained_root = fixture_root / "retained-workspace"
        retained_root.mkdir(mode=0o700)
        marker = retained_root / "first" / "partial.txt"
        render_error = RuntimeError("forced partial deterministic render")

        def fail_after_partial_render(*, output_root: Path) -> None:
            output_root.mkdir(parents=True)
            marker.write_text("preserve partial render\n", encoding="utf-8")
            raise render_error

        output = io.StringIO()
        try:
            with patch.object(
                check_determinism.tempfile,
                "mkdtemp",
                return_value=str(retained_root),
            ) as make_workspace, patch.object(
                check_determinism.tempfile,
                "TemporaryDirectory",
                side_effect=AssertionError(
                    "determinism caller must not auto-delete its workspace"
                ),
            ) as temporary_directory, patch.object(
                check_determinism,
                "render_all",
                side_effect=fail_after_partial_render,
            ), redirect_stdout(output), self.assertRaises(RuntimeError) as raised:
                check_determinism.main()

            self.assertIs(render_error, raised.exception)
            make_workspace.assert_called_once_with(
                prefix="stata-render-double-"
            )
            temporary_directory.assert_not_called()
            self.assertTrue(retained_root.is_dir())
            self.assertEqual(
                "preserve partial render\n",
                marker.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "deterministic-render workspace retained for explicit cleanup "
                f"at: {retained_root.resolve()}",
                output.getvalue(),
            )
        finally:
            shutil.rmtree(fixture_root)

    def test_determinism_reports_moved_outer_workspace(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="determinism-moved-workspace-test-")
        )
        retained_root = fixture_root / "retained-workspace"
        retained_root.mkdir(mode=0o700)
        moved_root = fixture_root / "moved-workspace"
        render_error = RuntimeError("forced moved deterministic render")

        def move_after_partial_render(*, output_root: Path) -> None:
            output_root.mkdir(parents=True)
            (output_root / "partial.txt").write_text(
                "preserve moved deterministic render\n",
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
                render_skills,
                "_descriptor_reported_path",
                return_value=None,
            ), patch.object(
                check_determinism,
                "render_all",
                side_effect=move_after_partial_render,
            ), redirect_stdout(output), self.assertRaises(RuntimeError) as raised:
                check_determinism.main()

            self.assertIs(render_error, raised.exception)
            self.assertFalse(retained_root.exists())
            self.assertEqual(
                "preserve moved deterministic render\n",
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
                f"at: {retained_root.resolve()}",
                output.getvalue(),
            )
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

    def test_caller_workspace_missing_parent_setup_closes_anchor_descriptor(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="caller-workspace-setup-test-")
        )
        workspace = fixture_root / "missing-parent" / "workspace"
        output = io.StringIO()
        before_fds = len(os.listdir("/dev/fd"))
        try:
            with redirect_stdout(output), self.assertRaisesRegex(
                render_skills.RenderTransactionError,
                "workspace parent is not an existing directory",
            ):
                with render_skills._retained_workspace_scope(
                    workspace,
                    "setup",
                ):
                    self.fail("invalid workspace setup must not yield")

            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))
            self.assertIn(
                "setup workspace retained for explicit cleanup at: "
                "unknown pathname",
                output.getvalue(),
            )
        finally:
            shutil.rmtree(fixture_root)

    def test_caller_workspace_close_interruption_preserves_render_failure(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="caller-workspace-close-test-")
        )
        retained_root = fixture_root / "retained-workspace"
        retained_root.mkdir(mode=0o700)
        render_error = RuntimeError("forced caller render failure")
        close_interruption = KeyboardInterrupt(
            "forced caller workspace close interruption"
        )
        real_close = render_skills.os.close
        close_calls: list[int] = []

        def close_then_interrupt_first(descriptor: int) -> None:
            close_calls.append(descriptor)
            real_close(descriptor)
            if len(close_calls) == 1:
                raise close_interruption

        output = io.StringIO()
        try:
            with patch.object(
                check_determinism.tempfile,
                "mkdtemp",
                return_value=str(retained_root),
            ), patch.object(
                check_determinism,
                "render_all",
                side_effect=render_error,
            ), patch.object(
                render_skills.os,
                "close",
                side_effect=close_then_interrupt_first,
            ), redirect_stdout(output), self.assertRaises(RuntimeError) as raised:
                check_determinism.main()

            self.assertIs(render_error, raised.exception)
            self.assertEqual(2, len(close_calls))
            self.assertEqual(2, len(set(close_calls)))
            for descriptor in close_calls:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertTrue(retained_root.is_dir())
            self.assertIn(
                "deterministic-render workspace retained for explicit cleanup "
                f"at: {retained_root.resolve()}",
                output.getvalue(),
            )
        finally:
            shutil.rmtree(fixture_root)

    def test_caller_workspace_reporting_interruption_preserves_render_failure(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="caller-workspace-report-test-")
        )
        retained_root = fixture_root / "retained-workspace"
        retained_root.mkdir(mode=0o700)
        render_error = RuntimeError("forced caller render failure")
        reporting_interruption = KeyboardInterrupt(
            "forced caller workspace reporting interruption"
        )
        before_fds = len(os.listdir("/dev/fd"))
        try:
            with patch.object(
                check_determinism.tempfile,
                "mkdtemp",
                return_value=str(retained_root),
            ), patch.object(
                check_determinism,
                "render_all",
                side_effect=render_error,
            ), patch.object(
                render_skills,
                "_descriptor_reported_path",
                side_effect=reporting_interruption,
            ), self.assertRaises(RuntimeError) as raised:
                check_determinism.main()

            self.assertIs(render_error, raised.exception)
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))
            self.assertIn(
                "deterministic-render workspace retention descriptor "
                "finalization also encountered: KeyboardInterrupt",
                getattr(raised.exception, "__notes__", ()),
            )
        finally:
            shutil.rmtree(fixture_root)

    def test_generated_drift_failure_retains_and_reports_outer_workspace(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="drift-preservation-test-")
        )
        retained_root = fixture_root / "retained-workspace"
        retained_root.mkdir(mode=0o700)
        build_root = fixture_root / "current-generated"
        build_root.mkdir()
        marker = retained_root / "generated" / "partial.txt"

        def fail_after_partial_render(*, output_root: Path) -> None:
            output_root.mkdir(parents=True)
            marker.write_text("preserve partial drift render\n", encoding="utf-8")
            raise RuntimeError("forced partial generated-drift render")

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
            ) as make_workspace, patch.object(
                lint_skill_pack.tempfile,
                "TemporaryDirectory",
                side_effect=AssertionError(
                    "generated-drift caller must not auto-delete its workspace"
                ),
            ) as temporary_directory, patch.object(
                render_skills,
                "render_all",
                side_effect=fail_after_partial_render,
            ), redirect_stdout(output):
                errors = lint_skill_pack.lint_generated_drift()

            self.assertEqual(
                [
                    "generated render failed: "
                    "forced partial generated-drift render"
                ],
                errors,
            )
            make_workspace.assert_called_once_with(
                prefix="stata-render-check-"
            )
            temporary_directory.assert_not_called()
            self.assertTrue(retained_root.is_dir())
            self.assertEqual(
                "preserve partial drift render\n",
                marker.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "generated-drift workspace retained for explicit cleanup "
                f"at: {retained_root.resolve()}",
                output.getvalue(),
            )
        finally:
            shutil.rmtree(fixture_root)

    def test_generated_drift_reports_moved_outer_workspace(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="drift-moved-workspace-test-")
        )
        retained_root = fixture_root / "retained-workspace"
        retained_root.mkdir(mode=0o700)
        moved_root = fixture_root / "moved-workspace"
        build_root = fixture_root / "current-generated"
        build_root.mkdir()

        def move_after_partial_render(*, output_root: Path) -> None:
            output_root.mkdir(parents=True)
            (output_root / "partial.txt").write_text(
                "preserve moved drift render\n",
                encoding="utf-8",
            )
            retained_root.rename(moved_root)
            raise RuntimeError("forced moved generated-drift render")

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
                    "forced moved generated-drift render"
                ],
                errors,
            )
            self.assertFalse(retained_root.exists())
            self.assertEqual(
                "preserve moved drift render\n",
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
                f"at: {retained_root.resolve()}",
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

    def test_recovery_lookup_interruption_still_restores_prior_tree(
        self,
    ) -> None:
        fixture_root = Path(
            tempfile.mkdtemp(prefix="rollback-setup-test-")
        )
        target = fixture_root / "generated"
        render_skills.render_all(output_root=target)
        prior = self.snapshot(target)
        placement_error = KeyboardInterrupt("forced placement interruption")
        lookup_error = SystemExit("forced first recovery identity interruption")
        real_rename = render_skills._rename_render_entry
        real_entry_identity = render_skills._entry_identity_at
        rename_calls = 0
        recovery_started = False
        lookup_interrupted = False
        restoration_attempted = False

        def interrupt_placement(
            parent: render_skills.RenderParentHandle,
            source_name: str,
            destination_name: str,
        ) -> None:
            nonlocal rename_calls, recovery_started, restoration_attempted
            rename_calls += 1
            if rename_calls == 2:
                recovery_started = True
                raise placement_error
            if recovery_started:
                restoration_attempted = True
            real_rename(parent, source_name, destination_name)

        def interrupt_first_recovery_lookup(
            parent: render_skills.RenderParentHandle,
            name: str,
        ) -> tuple[int, int] | None:
            nonlocal lookup_interrupted
            if recovery_started and not lookup_interrupted:
                lookup_interrupted = True
                raise lookup_error
            return real_entry_identity(parent, name)

        try:
            with patch.object(
                render_skills,
                "_rename_render_entry",
                side_effect=interrupt_placement,
            ), patch.object(
                render_skills,
                "_entry_identity_at",
                side_effect=interrupt_first_recovery_lookup,
            ), self.assertRaises(KeyboardInterrupt) as raised:
                render_skills.render_all(output_root=target)

            self.assertIs(placement_error, raised.exception)
            self.assertTrue(lookup_interrupted)
            self.assertTrue(restoration_attempted)
            self.assertEqual(prior, self.snapshot(target))
            self.assertIn(
                "render rollback setup also encountered: SystemExit",
                getattr(raised.exception, "__notes__", ()),
            )
        finally:
            shutil.rmtree(fixture_root)


if __name__ == "__main__":
    unittest.main()
