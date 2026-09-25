from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_skill_pack  # noqa: E402
import release_state  # noqa: E402
import render_skills  # noqa: E402


@contextmanager
def supplied_validation_workspace(work_root: Path):
    workspace = validate_skill_pack._retain_existing_validation_workspace(
        work_root
    )
    try:
        with patch.object(
            validate_skill_pack,
            "_create_validation_workspace",
            return_value=workspace,
        ):
            yield workspace
    finally:
        for descriptor in (
            workspace.work_descriptor,
            workspace.transaction_descriptor,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass


class ValidationReceiptCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validation_temp = TemporaryDirectory(
            prefix="validation-receipt-workspaces-"
        )
        self.addCleanup(self.validation_temp.cleanup)
        self.validation_temp_patch = patch.object(
            validate_skill_pack.tempfile,
            "gettempdir",
            return_value=self.validation_temp.name,
        )
        self.validation_temp_patch.start()
        self.addCleanup(self.validation_temp_patch.stop)

    def default_patches(self) -> list:
        return [
            patch.object(validate_skill_pack, "lint_repo", return_value=[]),
            patch.object(
                validate_skill_pack,
                "detect_stata_binary",
                return_value=Path("/Applications/Stata/StataBE.app"),
            ),
            patch.object(
                validate_skill_pack,
                "validate_core",
                return_value=[("sample", True, "")],
            ),
            patch.object(
                validate_skill_pack,
                "validate_packages",
                return_value=[("sample", True, "")],
            ),
            patch.object(
                validate_skill_pack,
                "validate_plugin_compile",
                return_value=(True, "", Path("/tmp/sample.plugin")),
            ),
        ]

    def test_failed_gate_invalidates_and_preserves_prior_receipt(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            receipt.write_text("stale", encoding="utf-8")
            contexts = self.default_patches()
            with contexts[0], contexts[1], patch.object(
                validate_skill_pack,
                "validate_core",
                return_value=[("sample", False, "failed")],
            ), contexts[3], contexts[4], patch.object(
                validate_skill_pack,
                "validation_state",
                return_value={
                    "source_sha256": "source",
                    "tree_sha256": "tree",
                },
            ), patch.object(
                validate_skill_pack,
                "write_validation_receipt",
            ) as write_receipt, patch.object(
                validate_skill_pack,
                "BUILD_ROOT",
                build_root,
            ):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )
                backups = list(
                    build_root.parent.glob(
                        f".{receipt.name}.backup-*"
                    )
                )
                backup_bytes = [
                    path.read_text(encoding="utf-8")
                    for path in backups
                ]
                public_receipt_absent = not receipt.exists()

        self.assertEqual(1, result)
        self.assertTrue(public_receipt_absent)
        self.assertEqual(["stale"], backup_bytes)
        write_receipt.assert_not_called()

    def test_invalidation_sync_failure_reports_prior_backup(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            receipt.write_text("prior receipt bytes\n", encoding="utf-8")
            real_fsync = release_state.os.fsync
            before_fds = len(os.listdir("/dev/fd"))
            output = io.StringIO()

            def sync_then_fail(descriptor: int) -> None:
                real_fsync(descriptor)
                raise OSError("forced receipt directory sync failure")

            with patch.object(
                validate_skill_pack,
                "BUILD_ROOT",
                build_root,
            ), patch.object(
                release_state.os,
                "fsync",
                side_effect=sync_then_fail,
            ), redirect_stdout(output):
                result = validate_skill_pack.main(
                    ["--invalidate-receipt"]
                )

            backups = list(
                build_root.parent.glob(
                    f".{receipt.name}.backup-*"
                )
            )
            self.assertEqual(1, result)
            self.assertFalse(receipt.exists())
            self.assertEqual(1, len(backups))
            self.assertEqual(
                "prior receipt bytes\n",
                backups[0].read_text(encoding="utf-8"),
            )
            self.assertIn(str(backups[0]), output.getvalue())
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))

    def test_filtered_validation_cannot_write_release_receipt(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            with patch.object(validate_skill_pack, "BUILD_ROOT", build_root):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--package",
                        "asdoc",
                        "--write-receipt",
                    ]
                )

        self.assertEqual(2, result)
        self.assertFalse(receipt.exists())

    def test_late_public_receipt_survives_writer_failure(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            contexts = self.default_patches()

            def create_late_receipt(**_kwargs) -> None:
                receipt.write_text(
                    "late receipt bytes\n",
                    encoding="utf-8",
                )
                raise ValueError("changed during receipt publication")

            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], patch.object(
                validate_skill_pack,
                "validation_state",
                return_value={
                    "source_sha256": "before",
                    "tree_sha256": "before",
                },
            ), patch.object(
                validate_skill_pack,
                "write_validation_receipt",
                side_effect=create_late_receipt,
            ), patch.object(
                validate_skill_pack,
                "BUILD_ROOT",
                build_root,
            ):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )
                receipt_bytes = receipt.read_text(encoding="utf-8")

        self.assertEqual(1, result)
        self.assertEqual("late receipt bytes\n", receipt_bytes)

    def test_cleanup_failure_prevents_receipt_and_reports_workdir(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            contexts = self.default_patches()
            output = io.StringIO()
            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], patch.object(
                validate_skill_pack,
                "validation_state",
                return_value={
                    "source_sha256": "source",
                    "tree_sha256": "tree",
                },
            ), patch.object(
                validate_skill_pack,
                "write_validation_receipt",
            ) as write_receipt, patch.object(
                validate_skill_pack,
                "BUILD_ROOT",
                build_root,
            ), patch.object(
                validate_skill_pack,
                "_remove_owned_validation_workspace",
                side_effect=OSError("forced cleanup failure"),
            ), redirect_stdout(output):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )

        self.assertEqual(1, result)
        self.assertFalse(receipt.exists())
        write_receipt.assert_not_called()
        self.assertIn(
            "validation workspace cleanup: FAIL",
            output.getvalue(),
        )
        self.assertIn(
            "validation workdir cleanup failed closed; inspect the "
            "preservation details",
            output.getvalue(),
        )
        retained = list(
            Path(self.validation_temp.name).glob(
                f"{validate_skill_pack.VALIDATION_TRANSACTION_PREFIX}*"
            )
        )
        self.assertEqual(1, len(retained))
        self.assertIn(str(retained[0]), output.getvalue())

    def test_keep_workdir_preserves_incomplete_workspace_on_base_exception(
        self,
    ) -> None:
        interruption_cases = (
            KeyboardInterrupt("forced keyboard interrupt"),
            SystemExit("forced system exit"),
            BaseException("forced base exception"),
        )
        for interruption in interruption_cases:
            with self.subTest(interruption=type(interruption).__name__):
                with TemporaryDirectory(
                    prefix="validation-receipt-"
                ) as temp_root:
                    build_root = Path(temp_root) / "build" / "generated"
                    build_root.parent.mkdir(parents=True)
                    receipt = build_root.parent / "validation-receipt.json"
                    receipt.write_text("stale", encoding="utf-8")
                    work_root = Path(temp_root) / "validation-work"
                    work_root.mkdir()
                    marker = work_root / "partial.txt"

                    def interrupt_validation() -> list[str]:
                        marker.write_text(
                            "partial validation state\n",
                            encoding="utf-8",
                        )
                        raise interruption

                    output = io.StringIO()
                    with patch.object(
                        validate_skill_pack,
                        "lint_repo",
                        side_effect=interrupt_validation,
                    ), patch.object(
                        validate_skill_pack,
                        "validation_state",
                        return_value={
                            "source_sha256": "source",
                            "tree_sha256": "tree",
                        },
                    ), patch.object(
                        validate_skill_pack,
                        "write_validation_receipt",
                    ) as write_receipt, patch.object(
                        validate_skill_pack,
                        "BUILD_ROOT",
                        build_root,
                    ), supplied_validation_workspace(
                        work_root
                    ), redirect_stdout(output), self.assertRaises(
                        type(interruption)
                    ) as raised:
                        validate_skill_pack.main(
                            [
                                "--suite",
                                "default",
                                "--write-receipt",
                                "--keep-workdir",
                            ]
                        )

                    self.assertIs(interruption, raised.exception)
                    self.assertEqual(
                        "partial validation state\n",
                        marker.read_text(encoding="utf-8"),
                    )
                    self.assertFalse(receipt.exists())
                    write_receipt.assert_not_called()
                    self.assertIn(
                        "validation workdir retained for explicit cleanup at: "
                        f"{work_root.resolve()}",
                        output.getvalue(),
                    )

    def test_interruption_without_keep_workdir_retains_owned_workspace(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            interruption = KeyboardInterrupt("forced keyboard interrupt")

            with patch.object(
                validate_skill_pack,
                "lint_repo",
                side_effect=interruption,
            ), patch.object(
                validate_skill_pack,
                "validation_state",
                return_value={
                    "source_sha256": "source",
                    "tree_sha256": "tree",
                },
            ), patch.object(
                validate_skill_pack,
                "write_validation_receipt",
            ) as write_receipt, patch.object(
                validate_skill_pack,
                "BUILD_ROOT",
                build_root,
            ), self.assertRaises(KeyboardInterrupt) as raised:
                validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )

            self.assertIs(interruption, raised.exception)
            retained = list(
                Path(self.validation_temp.name).glob(
                    f"{validate_skill_pack.VALIDATION_TRANSACTION_PREFIX}*"
                )
            )
            self.assertEqual(1, len(retained))
            self.assertTrue(
                (
                    retained[0]
                    / validate_skill_pack.VALIDATION_WORKDIR_NAME
                ).is_dir()
            )
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()

    def test_changed_workdir_identity_is_preserved_and_blocks_receipt(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            work_root = Path(temp_root) / "validation-work"
            work_root.mkdir()
            displaced_work_root = Path(temp_root) / "displaced-validation-work"
            owned_marker = work_root / "owned.txt"
            owned_marker.write_text("owned validation bytes\n", encoding="utf-8")
            replacement_marker = work_root / "replacement.txt"

            def replace_workdir_path() -> list[str]:
                work_root.rename(displaced_work_root)
                work_root.mkdir()
                replacement_marker.write_text(
                    "replacement bytes\n",
                    encoding="utf-8",
                )
                return []

            contexts = self.default_patches()
            output = io.StringIO()
            with patch.object(
                validate_skill_pack,
                "lint_repo",
                side_effect=replace_workdir_path,
            ), contexts[1], contexts[2], contexts[3], contexts[4], patch.object(
                validate_skill_pack,
                "validation_state",
                return_value={
                    "source_sha256": "source",
                    "tree_sha256": "tree",
                },
            ), patch.object(
                validate_skill_pack,
                "write_validation_receipt",
            ) as write_receipt, patch.object(
                validate_skill_pack,
                "BUILD_ROOT",
                build_root,
            ), supplied_validation_workspace(
                work_root
            ), redirect_stdout(output):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )

            self.assertEqual(1, result)
            self.assertEqual(
                "owned validation bytes\n",
                (displaced_work_root / "owned.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                "replacement bytes\n",
                replacement_marker.read_text(encoding="utf-8"),
            )
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()
            self.assertIn(
                "validation workdir identity changed before cleanup",
                output.getvalue(),
            )

    def test_changed_file_at_cleanup_move_is_preserved(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            build_root = root / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            work_root = root / "validation-work"
            work_root.mkdir()
            (work_root / "owned.txt").write_text(
                "owned validation bytes\n",
                encoding="utf-8",
            )
            real_verify = render_skills._verify_directory_descriptor_tree
            changed = False
            verification_count = 0

            def change_file_after_first_retention_check(
                descriptor: int,
                display_path: Path,
                expected_entries: dict[str, render_skills.RenderTreeEntry],
                relative_parts: tuple[str, ...] = (),
            ) -> None:
                nonlocal changed, verification_count
                real_verify(
                    descriptor,
                    display_path,
                    expected_entries,
                    relative_parts,
                )
                if not relative_parts:
                    verification_count += 1
                if not relative_parts and verification_count == 1:
                    os.rename(
                        "owned.txt",
                        "accepted.txt",
                        src_dir_fd=descriptor,
                        dst_dir_fd=descriptor,
                    )
                    replacement_descriptor = os.open(
                        "owned.txt",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=descriptor,
                    )
                    try:
                        os.write(
                            replacement_descriptor,
                            b"replacement validation bytes\n",
                        )
                    finally:
                        os.close(replacement_descriptor)
                    changed = True

            contexts = self.default_patches()
            output = io.StringIO()
            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], patch.object(
                validate_skill_pack,
                "validation_state",
                return_value={
                    "source_sha256": "source",
                    "tree_sha256": "tree",
                },
            ), patch.object(
                validate_skill_pack,
                "write_validation_receipt",
            ) as write_receipt, patch.object(
                validate_skill_pack,
                "BUILD_ROOT",
                build_root,
            ), supplied_validation_workspace(work_root), patch.object(
                render_skills,
                "_verify_directory_descriptor_tree",
                side_effect=change_file_after_first_retention_check,
            ), redirect_stdout(output):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )

            self.assertTrue(changed)
            self.assertEqual(1, result)
            self.assertEqual(
                "owned validation bytes\n",
                (work_root / "accepted.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "replacement validation bytes\n",
                (work_root / "owned.txt").read_text(encoding="utf-8"),
            )
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()
            self.assertIn(
                "changed during private pre-delete verification",
                output.getvalue(),
            )

    def test_validation_phase_uses_descriptor_retained_workdir(
        self,
    ) -> None:
        workspace = validate_skill_pack._create_validation_workspace()
        moved_transaction = workspace.transaction_root.with_name(
            "moved-validation-transaction"
        )
        workspace.transaction_root.rename(moved_transaction)
        replacement_transaction = workspace.transaction_root
        replacement_transaction.mkdir(mode=0o700)
        replacement_work = replacement_transaction / (
            validate_skill_pack.VALIDATION_WORKDIR_NAME
        )
        replacement_work.mkdir(mode=0o700)
        replacement_marker = replacement_work / "replacement.txt"
        replacement_marker.write_text(
            "replacement validation bytes\n",
            encoding="utf-8",
        )
        received_roots: list[Path] = []

        def write_validation_probe(work_root: Path):
            received_roots.append(work_root)
            probe = work_root / "phase-probe"
            probe.mkdir()
            (probe / "result.txt").write_text(
                "accepted workspace result\n",
                encoding="utf-8",
            )
            return True, "", probe / "result.txt"

        output = io.StringIO()
        with patch.object(
            validate_skill_pack,
            "_create_validation_workspace",
            return_value=workspace,
        ), patch.object(
            validate_skill_pack,
            "validate_plugin_compile",
            side_effect=write_validation_probe,
        ), redirect_stdout(output):
            result = validate_skill_pack.main(
                ["--suite", "plugin-compile", "--keep-workdir"]
            )

        self.assertEqual(0, result)
        self.assertEqual([Path(".")], received_roots)
        accepted_probe = (
            moved_transaction
            / validate_skill_pack.VALIDATION_WORKDIR_NAME
            / "phase-probe"
            / "result.txt"
        )
        self.assertEqual(
            "accepted workspace result\n",
            accepted_probe.read_text(encoding="utf-8"),
        )
        self.assertFalse((replacement_work / "phase-probe").exists())
        self.assertEqual(
            "replacement validation bytes\n",
            replacement_marker.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "validation transaction retained for explicit cleanup at: "
            f"{moved_transaction}",
            output.getvalue(),
        )

    def test_unverified_fallback_path_blocks_receipt_and_is_not_reported(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            approved_parent = root / "approved"
            approved_parent.mkdir()
            displaced_parent = root / "displaced"
            build_root = root / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            replacement: Path | None = None

            def move_parent_and_reuse_transaction_name() -> list[str]:
                nonlocal replacement
                approved_parent.rename(displaced_parent)
                approved_parent.mkdir()
                transaction_root = next(displaced_parent.iterdir())
                replacement = approved_parent / transaction_root.name
                replacement.mkdir()
                (replacement / "valuable.txt").write_text(
                    "replacement bytes\n",
                    encoding="utf-8",
                )
                return []

            contexts = self.default_patches()
            output = io.StringIO()
            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(approved_parent),
            ), patch.object(
                validate_skill_pack,
                "lint_repo",
                side_effect=move_parent_and_reuse_transaction_name,
            ), patch.object(
                validate_skill_pack,
                "_descriptor_reported_path",
                return_value=None,
            ), patch.object(
                validate_skill_pack,
                "validation_state",
                return_value={
                    "source_sha256": "source",
                    "tree_sha256": "tree",
                },
            ), patch.object(
                validate_skill_pack,
                "write_validation_receipt",
            ) as write_receipt, patch.object(
                validate_skill_pack,
                "BUILD_ROOT",
                build_root,
            ), redirect_stdout(output):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )

            self.assertEqual(1, result)
            self.assertIsNotNone(replacement, output.getvalue())
            assert replacement is not None
            self.assertEqual(
                "replacement bytes\n",
                (replacement / "valuable.txt").read_text(encoding="utf-8"),
            )
            self.assertNotIn(str(replacement), output.getvalue())
            self.assertIn("unknown pathname (device=", output.getvalue())
            self.assertIn(
                "no verified cleanup pathname is available",
                output.getvalue(),
            )
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()

    def test_make_validate_invalidates_receipt_before_failed_check(self) -> None:
        with TemporaryDirectory(prefix="validation-make-") as temp_root:
            root = Path(temp_root)
            shutil.copyfile(REPO_ROOT / "Makefile", root / "Makefile")
            shutil.copytree(
                REPO_ROOT / "scripts",
                root / "scripts",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            subprocess.run(
                ["git", "init"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            subprocess.run(
                ["git", "add", ".gitignore", "Makefile", "scripts"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            build_root = root / "build" / "generated"
            for folder in release_state.SKILL_FOLDERS:
                skill_root = build_root / folder
                (skill_root / "agents").mkdir(parents=True)
                (skill_root / "SKILL.md").write_text(
                    f"# {folder}\n",
                    encoding="utf-8",
                )
                (skill_root / "PROVENANCE.md").write_text(
                    "# Provenance\n",
                    encoding="utf-8",
                )
                (skill_root / "agents" / "openai.yaml").write_text(
                    f"display_name: {folder}\n",
                    encoding="utf-8",
                )
            receipt = build_root.parent / "validation-receipt.json"
            release_state.write_validation_receipt(
                build_root=build_root,
                receipt_path=receipt,
                repo_root=root,
            )
            self.assertTrue(receipt.is_file())
            prior_receipt = receipt.read_bytes()
            fake_uv = root / "fake-uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                'case "$*" in\n'
                '  *"scripts/validate_skill_pack.py --invalidate-receipt"*)\n'
                f"    exec '{sys.executable}' "
                "scripts/validate_skill_pack.py --invalidate-receipt\n"
                "    ;;\n"
                "esac\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_uv.chmod(0o755)

            result = subprocess.run(
                [
                    "make",
                    "validate",
                    f"UV={fake_uv}",
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertFalse(receipt.exists())
            backups = list(
                receipt.parent.glob(
                    f".{receipt.name}.backup-*"
                )
            )
            self.assertEqual(1, len(backups))
            self.assertEqual(prior_receipt, backups[0].read_bytes())
            self.assertIn(
                "prior validation receipt retained",
                result.stdout,
            )
            self.assertIn("lock --check --offline", result.stdout)

    def test_make_validate_refuses_symlinked_build_before_invalidation(self) -> None:
        with TemporaryDirectory(prefix="validation-make-symlink-") as temp_root:
            root = Path(temp_root) / "repository"
            root.mkdir()
            shutil.copyfile(REPO_ROOT / "Makefile", root / "Makefile")
            external_build = Path(temp_root) / "external-build"
            external_build.mkdir()
            protected = external_build / "validation-receipt.json"
            protected.write_text("protected receipt\n", encoding="utf-8")
            (root / "build").symlink_to(
                external_build,
                target_is_directory=True,
            )

            result = subprocess.run(
                ["make", "validate", "UV=false"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("build must not be a symlink", result.stdout)
            self.assertEqual(
                "protected receipt\n",
                protected.read_text(encoding="utf-8"),
            )

    def test_symlinked_receipt_is_rejected_without_unlinking_target(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            build_root = root / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            target = root / "protected.txt"
            target.write_text("protected\n", encoding="utf-8")
            receipt = build_root.parent / "validation-receipt.json"
            receipt.symlink_to(target)

            with patch.object(validate_skill_pack, "BUILD_ROOT", build_root):
                result = validate_skill_pack.main(
                    ["--suite", "default", "--write-receipt"]
                )

            self.assertEqual(2, result)
            self.assertTrue(receipt.is_symlink())
            self.assertEqual("protected\n", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
