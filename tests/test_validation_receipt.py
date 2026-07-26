from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import ANY, Mock, patch


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

    def test_successful_default_gate_writes_receipt(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            contexts = self.default_patches()
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
            ):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )

        self.assertEqual(0, result)
        self.assertEqual(
            [],
            list(Path(self.validation_temp.name).iterdir()),
        )
        write_receipt.assert_called_once_with(
            build_root=build_root,
            receipt_path=receipt,
            expected_state={
                "source_sha256": "source",
                "tree_sha256": "tree",
            },
            transaction=ANY,
        )

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

    def test_state_change_during_gate_prevents_receipt(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            contexts = self.default_patches()
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
                side_effect=ValueError("changed during validation"),
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

        self.assertEqual(1, result)
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

    def test_workdir_change_at_quarantine_is_preserved_and_blocks_receipt(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            build_root = root / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            work_root = root / "validation-work"
            work_root.mkdir()
            displaced_work_root = root / "displaced-validation-work"
            owned_marker = work_root / "owned.txt"
            owned_marker.write_text("owned validation bytes\n", encoding="utf-8")
            real_retain = validate_skill_pack._retain_owned_directory
            changed = False

            def change_path_at_retention(
                directory: Path,
                expected_device: int,
                expected_inode: int,
                expected_entries: tuple[render_skills.RenderTreeEntry, ...],
                *,
                expected_mode: int | None = None,
                parent_descriptor: int | None = None,
            ) -> Path:
                nonlocal changed
                if not changed:
                    work_root.rename(displaced_work_root)
                    work_root.mkdir()
                    (work_root / "replacement.txt").write_text(
                        "replacement bytes\n",
                        encoding="utf-8",
                    )
                    changed = True
                return real_retain(
                    directory,
                    expected_device,
                    expected_inode,
                    expected_entries,
                    expected_mode=expected_mode,
                    parent_descriptor=parent_descriptor,
                )

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
                validate_skill_pack,
                "_retain_owned_directory",
                side_effect=change_path_at_retention,
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
                (displaced_work_root / "owned.txt").read_text(
                    encoding="utf-8"
                ),
            )
            replacement_markers = list(root.rglob("replacement.txt"))
            self.assertEqual(1, len(replacement_markers))
            self.assertEqual(
                "replacement bytes\n",
                replacement_markers[0].read_text(encoding="utf-8"),
            )
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()
            self.assertIn(
                "no longer matches its captured identity",
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

    def test_late_cleanup_entry_restores_complete_public_workspace(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            build_root = root / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            work_root = root / "validation-work"
            work_root.mkdir()
            for name in ("alpha.txt", "beta.txt"):
                (work_root / name).write_text(
                    f"owned {name}\n",
                    encoding="utf-8",
                )
            real_verify = render_skills._verify_directory_descriptor_tree
            injected = False
            verification_count = 0

            def add_late_entry_after_first_retention_check(
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
                if not relative_parts:
                    verification_count += 1
                if not relative_parts and verification_count == 1:
                    late_descriptor = os.open(
                        "late.txt",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=descriptor,
                    )
                    try:
                        os.write(late_descriptor, b"late validation bytes\n")
                    finally:
                        os.close(late_descriptor)
                    injected = True

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
                side_effect=add_late_entry_after_first_retention_check,
            ), redirect_stdout(output):
                result = validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )

            self.assertTrue(injected)
            self.assertEqual(1, result)
            self.assertEqual(
                {
                    "alpha.txt": "owned alpha.txt\n",
                    "beta.txt": "owned beta.txt\n",
                    "late.txt": "late validation bytes\n",
                },
                {
                    path.name: path.read_text(encoding="utf-8")
                    for path in work_root.iterdir()
                },
            )
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()
            self.assertIn(
                "changed during private pre-delete verification",
                output.getvalue(),
            )

    def test_cleanup_interruption_preserves_original_exception_and_closes_fds(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            build_root = root / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            work_root = root / "validation-work"
            work_root.mkdir()
            original = KeyboardInterrupt("validation interrupted")
            cleanup = SystemExit("cleanup interrupted")

            output = io.StringIO()
            with patch.object(
                validate_skill_pack,
                "lint_repo",
                side_effect=original,
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
            ) as workspace, patch.object(
                validate_skill_pack,
                "_retain_validation_workdir",
                side_effect=cleanup,
            ), redirect_stdout(output), self.assertRaises(
                KeyboardInterrupt
            ) as raised:
                validate_skill_pack.main(
                    [
                        "--suite",
                        "default",
                        "--write-receipt",
                    ]
                )

            self.assertIs(original, raised.exception)
            for descriptor in (
                workspace.work_descriptor,
                workspace.transaction_descriptor,
            ):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()
            self.assertIn(
                "preserving the active validation interruption",
                output.getvalue(),
            )

    def test_cleanup_base_exception_closes_fds_before_reraise(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            work_root = root / "validation-work"
            work_root.mkdir()
            cleanup = KeyboardInterrupt("cleanup interrupted")

            contexts = self.default_patches()
            with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4], patch.object(
                validate_skill_pack,
                "_create_validation_workspace",
                return_value=validate_skill_pack._retain_existing_validation_workspace(
                    work_root
                ),
            ) as create_workspace, patch.object(
                validate_skill_pack,
                "_retain_validation_workdir",
                side_effect=cleanup,
            ), self.assertRaises(KeyboardInterrupt) as raised:
                validate_skill_pack.main(["--suite", "default"])

            self.assertIs(cleanup, raised.exception)
            workspace = create_workspace.return_value
            for descriptor in (
                workspace.work_descriptor,
                workspace.transaction_descriptor,
            ):
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_unrelated_handled_exception_does_not_hide_cleanup_interruption(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            build_root = root / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            work_root = root / "validation-work"
            work_root.mkdir()
            cleanup = KeyboardInterrupt("cleanup interrupted")
            contexts = self.default_patches()
            try:
                raise ValueError("unrelated caller exception")
            except ValueError:
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
                    validate_skill_pack,
                    "_retain_validation_workdir",
                    side_effect=cleanup,
                ), self.assertRaises(KeyboardInterrupt) as raised:
                    validate_skill_pack.main(
                        [
                            "--suite",
                            "default",
                            "--write-receipt",
                        ]
                    )

            self.assertIs(cleanup, raised.exception)
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()

    def test_finalization_reporting_does_not_replace_validation_interruption(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            work_root = root / "validation-work"
            work_root.mkdir()
            validation_interruption = KeyboardInterrupt(
                "validation interrupted"
            )
            reporting_interruption = SystemExit("reporting interrupted")
            before_fds = len(os.listdir("/dev/fd"))

            with patch.object(
                validate_skill_pack,
                "lint_repo",
                side_effect=validation_interruption,
            ), patch.object(
                validate_skill_pack,
                "_retain_validation_workdir",
                side_effect=OSError("retention failed"),
            ), patch.object(
                validate_skill_pack,
                "_descriptor_location",
                side_effect=reporting_interruption,
            ), supplied_validation_workspace(
                work_root
            ), self.assertRaises(KeyboardInterrupt) as raised:
                validate_skill_pack.main(["--suite", "static"])

            self.assertIs(validation_interruption, raised.exception)
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))

    def test_transaction_change_before_open_is_rejected_and_preserved(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            build_root = root / "build" / "generated"
            build_root.parent.mkdir(parents=True)
            receipt = build_root.parent / "validation-receipt.json"
            displaced = root / "displaced-validation-transaction"
            real_open = validate_skill_pack.os.open
            changed = False
            replacement: Path | None = None

            def change_then_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal changed, replacement
                if (
                    dir_fd is not None
                    and isinstance(path, str)
                    and path.startswith(
                        validate_skill_pack.VALIDATION_TRANSACTION_PREFIX
                    )
                    and not changed
                ):
                    os.rename(
                        path,
                        displaced.name,
                        src_dir_fd=dir_fd,
                        dst_dir_fd=dir_fd,
                    )
                    os.mkdir(path, mode=0o700, dir_fd=dir_fd)
                    replacement = root / path
                    (replacement / "replacement.txt").write_text(
                        "replacement workspace bytes\n",
                        encoding="utf-8",
                    )
                    changed = True
                return real_open(
                    path,
                    flags,
                    mode,
                    dir_fd=dir_fd,
                )

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "open",
                side_effect=change_then_open,
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

            self.assertTrue(changed)
            self.assertEqual(1, result)
            self.assertTrue(displaced.is_dir())
            self.assertIsNotNone(replacement, output.getvalue())
            assert replacement is not None
            self.assertEqual(
                "replacement workspace bytes\n",
                (replacement / "replacement.txt").read_text(
                    encoding="utf-8",
                ),
            )
            self.assertFalse(receipt.exists())
            write_receipt.assert_not_called()
            self.assertIn(
                "transaction root changed while opening",
                output.getvalue(),
            )

    def test_transaction_change_before_first_stat_is_rejected_and_preserved(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            displaced = root / "displaced-validation-transaction"
            real_stat = validate_skill_pack.os.stat
            changed = False
            replacement: Path | None = None

            def change_then_stat(
                path,
                *,
                dir_fd=None,
                follow_symlinks=True,
            ):
                nonlocal changed, replacement
                metadata = real_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )
                if (
                    dir_fd is not None
                    and isinstance(path, str)
                    and path.startswith(
                        validate_skill_pack.VALIDATION_TRANSACTION_PREFIX
                    )
                    and not changed
                ):
                    os.rename(
                        path,
                        displaced.name,
                        src_dir_fd=dir_fd,
                        dst_dir_fd=dir_fd,
                    )
                    os.mkdir(path, mode=0o700, dir_fd=dir_fd)
                    replacement = root / path
                    (replacement / "replacement.txt").write_text(
                        "replacement transaction bytes\n",
                        encoding="utf-8",
                    )
                    replacement.chmod(0o500)
                    changed = True
                return metadata

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "stat",
                side_effect=change_then_stat,
            ), redirect_stdout(output):
                result = validate_skill_pack.main(["--suite", "static"])

            self.assertTrue(changed)
            self.assertEqual(1, result)
            self.assertTrue(displaced.is_dir())
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertEqual(
                "replacement transaction bytes\n",
                (replacement / "replacement.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(0o500, replacement.stat().st_mode & 0o777)
            self.assertIn(
                "transaction root changed while opening",
                output.getvalue(),
            )
            self.assertNotIn(
                f"incomplete validation transaction location: {replacement}",
                output.getvalue(),
            )
            displaced.chmod(0o700)

    def test_workdir_change_before_open_is_rejected_without_chmod(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            real_stat = validate_skill_pack.os.stat
            changed = False
            replacement: Path | None = None
            displaced: Path | None = None

            def change_work_after_stat(
                path,
                *,
                dir_fd=None,
                follow_symlinks=True,
            ):
                nonlocal changed, replacement, displaced
                metadata = real_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )
                if (
                    dir_fd is not None
                    and path == validate_skill_pack.VALIDATION_WORKDIR_NAME
                    and not changed
                ):
                    transaction_path = next(
                        candidate
                        for candidate in root.iterdir()
                        if candidate.name.startswith(
                            validate_skill_pack.VALIDATION_TRANSACTION_PREFIX
                        )
                    )
                    displaced = transaction_path / "displaced-work"
                    os.rename(
                        path,
                        displaced.name,
                        src_dir_fd=dir_fd,
                        dst_dir_fd=dir_fd,
                    )
                    os.mkdir(path, mode=0o700, dir_fd=dir_fd)
                    replacement = transaction_path / path
                    (replacement / "replacement.txt").write_text(
                        "replacement work bytes\n",
                        encoding="utf-8",
                    )
                    replacement.chmod(0o500)
                    changed = True
                return metadata

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "stat",
                side_effect=change_work_after_stat,
            ), redirect_stdout(output):
                result = validate_skill_pack.main(["--suite", "static"])

            self.assertTrue(changed)
            self.assertEqual(1, result)
            self.assertIsNotNone(displaced)
            self.assertIsNotNone(replacement)
            assert displaced is not None
            assert replacement is not None
            self.assertTrue(displaced.is_dir())
            self.assertEqual(
                "replacement work bytes\n",
                (replacement / "replacement.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(0o500, replacement.stat().st_mode & 0o777)
            self.assertIn(
                "validation workdir changed while opening it",
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

    def test_setup_interruption_reports_uncertain_candidate_and_closes_fds(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            real_stat = validate_skill_pack.os.stat
            interruption = KeyboardInterrupt("setup interrupted")
            before_fds = len(os.listdir("/dev/fd"))

            def interrupt_first_transaction_stat(
                path,
                *,
                dir_fd=None,
                follow_symlinks=True,
            ):
                if (
                    dir_fd is not None
                    and isinstance(path, str)
                    and path.startswith(
                        validate_skill_pack.VALIDATION_TRANSACTION_PREFIX
                    )
                ):
                    raise interruption
                return real_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "stat",
                side_effect=interrupt_first_transaction_stat,
            ), redirect_stdout(output), self.assertRaises(
                KeyboardInterrupt
            ) as raised:
                validate_skill_pack.main(["--suite", "static"])

            self.assertIs(interruption, raised.exception)
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))
            retained = list(
                root.glob(
                    f"{validate_skill_pack.VALIDATION_TRANSACTION_PREFIX}*"
                )
            )
            self.assertEqual(1, len(retained))
            self.assertIn(
                "transaction creation was incomplete",
                output.getvalue(),
            )
            self.assertIn(
                f"candidate path was {retained[0].resolve()}",
                output.getvalue(),
            )
            retained[0].chmod(0o700)

    def test_setup_close_interruption_does_not_replace_original_or_skip_closes(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            setup_interruption = KeyboardInterrupt("setup interrupted")
            close_interruption = SystemExit("close interrupted")
            real_close = validate_skill_pack.os.close
            close_calls: list[int] = []
            before_fds = len(os.listdir("/dev/fd"))

            def close_all_with_first_interruption(descriptor: int) -> None:
                close_calls.append(descriptor)
                real_close(descriptor)
                if len(close_calls) == 1:
                    raise close_interruption

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "fsync",
                side_effect=setup_interruption,
            ), patch.object(
                validate_skill_pack.os,
                "close",
                side_effect=close_all_with_first_interruption,
            ), redirect_stdout(output), self.assertRaises(
                KeyboardInterrupt
            ) as raised:
                validate_skill_pack._create_validation_workspace()

            self.assertIs(setup_interruption, raised.exception)
            self.assertEqual(3, len(close_calls))
            self.assertEqual(3, len(set(close_calls)))
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))
            self.assertIn(
                "setup finalization also encountered SystemExit",
                output.getvalue(),
            )

    def test_workdir_content_after_fchmod_blocks_workspace_setup(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            real_fchmod = validate_skill_pack.os.fchmod
            fchmod_700_calls = 0
            before_fds = len(os.listdir("/dev/fd"))

            def add_content_after_work_fchmod(
                descriptor: int,
                mode: int,
            ) -> None:
                nonlocal fchmod_700_calls
                real_fchmod(descriptor, mode)
                if mode != 0o700:
                    return
                fchmod_700_calls += 1
                if fchmod_700_calls != 2:
                    return
                marker_descriptor = os.open(
                    "valuable.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=descriptor,
                )
                try:
                    os.write(marker_descriptor, b"preserve marker\n")
                finally:
                    os.close(marker_descriptor)

            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "fchmod",
                side_effect=add_content_after_work_fchmod,
            ), self.assertRaisesRegex(
                OSError,
                "workdir changed during initialization",
            ):
                validate_skill_pack._create_validation_workspace()

            retained = list(
                root.glob(
                    f"{validate_skill_pack.VALIDATION_TRANSACTION_PREFIX}*"
                )
            )
            self.assertEqual(1, len(retained))
            self.assertEqual(
                "preserve marker\n",
                (retained[0] / "work" / "valuable.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))

    def test_nonempty_retained_workdir_is_rejected_before_phase(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            work_root = Path(temp_root) / "validation-work"
            work_root.mkdir()
            marker = work_root / "valuable.txt"
            marker.write_text("preserve marker\n", encoding="utf-8")
            validate_core = Mock(
                return_value=[("sample", True, "")]
            )

            with patch.object(
                validate_skill_pack,
                "detect_stata_binary",
                return_value=Path("/fake/stata"),
            ), patch.object(
                validate_skill_pack,
                "validate_core",
                validate_core,
            ), supplied_validation_workspace(work_root):
                result = validate_skill_pack.main(["--suite", "core"])

            self.assertEqual(1, result)
            validate_core.assert_not_called()
            self.assertEqual(
                "preserve marker\n",
                marker.read_text(encoding="utf-8"),
            )

    def test_setup_close_interruption_before_transaction_path_is_reported(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            setup_error = OSError("temp parent metadata failed")
            close_interruption = SystemExit("close interrupted")
            real_close = validate_skill_pack.os.close
            close_calls: list[int] = []
            before_fds = len(os.listdir("/dev/fd"))

            def close_then_interrupt(descriptor: int) -> None:
                close_calls.append(descriptor)
                real_close(descriptor)
                raise close_interruption

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "fstat",
                side_effect=setup_error,
            ), patch.object(
                validate_skill_pack.os,
                "close",
                side_effect=close_then_interrupt,
            ), redirect_stdout(output), self.assertRaises(OSError) as raised:
                validate_skill_pack._create_validation_workspace()

            self.assertIs(setup_error, raised.exception)
            self.assertIsInstance(raised.exception, OSError)
            self.assertEqual(1, len(close_calls))
            self.assertEqual(1, len(set(close_calls)))
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))
            self.assertEqual([], list(root.iterdir()))
            self.assertIn(
                "before any validation transaction path was created",
                output.getvalue(),
            )
            self.assertIn(
                "setup finalization also encountered SystemExit",
                output.getvalue(),
            )

    def test_setup_location_interruption_does_not_replace_original_or_skip_closes(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            setup_interruption = KeyboardInterrupt("setup interrupted")
            location_interruption = SystemExit("location interrupted")
            real_close = validate_skill_pack.os.close
            close_calls: list[int] = []
            before_fds = len(os.listdir("/dev/fd"))

            def record_close(descriptor: int) -> None:
                close_calls.append(descriptor)
                real_close(descriptor)

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "fsync",
                side_effect=setup_interruption,
            ), patch.object(
                validate_skill_pack,
                "_descriptor_location",
                side_effect=location_interruption,
            ), patch.object(
                validate_skill_pack.os,
                "close",
                side_effect=record_close,
            ), redirect_stdout(output), self.assertRaises(
                KeyboardInterrupt
            ) as raised:
                validate_skill_pack._create_validation_workspace()

            self.assertIs(setup_interruption, raised.exception)
            self.assertEqual(3, len(close_calls))
            self.assertEqual(3, len(set(close_calls)))
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))
            self.assertIn(
                "recovery-location reporting was interrupted",
                output.getvalue(),
            )
            self.assertIn(
                "setup finalization also encountered SystemExit",
                output.getvalue(),
            )

    def test_setup_diagnostic_interruption_does_not_replace_original(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            setup_interruption = KeyboardInterrupt("setup interrupted")
            diagnostic_interruption = SystemExit("diagnostic interrupted")
            before_fds = len(os.listdir("/dev/fd"))

            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(root),
            ), patch.object(
                validate_skill_pack.os,
                "fsync",
                side_effect=setup_interruption,
            ), patch(
                "builtins.print",
                side_effect=diagnostic_interruption,
            ), self.assertRaises(KeyboardInterrupt) as raised:
                validate_skill_pack._create_validation_workspace()

            self.assertIs(setup_interruption, raised.exception)
            self.assertEqual(before_fds, len(os.listdir("/dev/fd")))

    def test_symlinked_temp_parent_is_resolved_before_workspace_creation(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            real_temp_parent = root / "real-temp"
            real_temp_parent.mkdir()
            temp_alias = root / "temp-alias"
            temp_alias.symlink_to(real_temp_parent, target_is_directory=True)

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(temp_alias),
            ), patch.object(
                validate_skill_pack,
                "lint_repo",
                return_value=[],
            ), redirect_stdout(output):
                result = validate_skill_pack.main(["--suite", "static"])

            self.assertEqual(0, result)
            self.assertEqual([], list(real_temp_parent.iterdir()))
            self.assertNotIn(
                "retained for explicit cleanup",
                output.getvalue(),
            )

    def test_unrelated_cleanup_sibling_does_not_block_workspace_cleanup(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            temp_parent = Path(temp_root)
            unrelated = temp_parent / ".render-cleanup-stale"
            unrelated.mkdir()

            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(temp_parent),
            ), patch.object(
                validate_skill_pack,
                "lint_repo",
                return_value=[],
            ):
                result = validate_skill_pack.main(["--suite", "static"])

            self.assertEqual(0, result)
            retained = [
                path
                for path in temp_parent.iterdir()
                if path != unrelated
            ]
            self.assertEqual([], retained)
            self.assertTrue(unrelated.is_dir())

    def test_parent_move_reports_descriptor_derived_preservation_path(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            root = Path(temp_root)
            approved_parent = root / "approved"
            approved_parent.mkdir()
            displaced_parent = root / "displaced"
            moved = False

            def move_temp_parent() -> list[str]:
                nonlocal moved
                approved_parent.rename(displaced_parent)
                approved_parent.mkdir()
                moved = True
                return []

            output = io.StringIO()
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=str(approved_parent),
            ), patch.object(
                validate_skill_pack,
                "lint_repo",
                side_effect=move_temp_parent,
            ), redirect_stdout(output):
                result = validate_skill_pack.main(
                    ["--suite", "static", "--keep-workdir"]
                )

            self.assertTrue(moved)
            self.assertEqual(0, result)
            retained_workdirs = list(
                displaced_parent.glob(
                    f"{validate_skill_pack.VALIDATION_TRANSACTION_PREFIX}*/"
                    f"{validate_skill_pack.VALIDATION_WORKDIR_NAME}"
                )
            )
            self.assertEqual(1, len(retained_workdirs))
            transaction_root = retained_workdirs[0].parent
            self.assertIn(
                "validation transaction retained for explicit cleanup at: "
                f"{transaction_root.resolve()}",
                output.getvalue(),
            )
            self.assertIn(
                "validation workdir retained for inspection at: "
                f"{retained_workdirs[0].resolve()}",
                output.getvalue(),
            )
            self.assertNotIn(str(approved_parent.resolve()), output.getvalue())

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

    def test_workdir_open_failure_closes_both_descriptors(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            work_root = Path(temp_root) / "validation-work"
            work_root.mkdir()
            metadata = work_root.stat()
            before = len(os.listdir("/dev/fd"))

            with patch.object(
                validate_skill_pack.os,
                "fstat",
                side_effect=OSError("forced fstat failure"),
            ), self.assertRaisesRegex(OSError, "forced fstat failure"):
                validate_skill_pack._open_validation_workdir(
                    work_root,
                    (metadata.st_dev, metadata.st_ino),
                )

            self.assertEqual(before, len(os.listdir("/dev/fd")))

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

    def test_receipt_cli_override_cannot_target_repository_files(self) -> None:
        readme = REPO_ROOT / "README.md"
        before = readme.read_bytes()
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            validate_skill_pack.main(
                [
                    "--suite",
                    "default",
                    "--write-receipt",
                    "--receipt",
                    str(readme),
                ]
            )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments", stderr.getvalue())
        self.assertEqual(before, readme.read_bytes())

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
