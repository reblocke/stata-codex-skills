from __future__ import annotations

from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import stat
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import publish_local  # noqa: E402
import release_state  # noqa: E402


def write_skill_tree(root: Path, label: str) -> None:
    for folder in release_state.SKILL_FOLDERS:
        skill_root = root / folder
        (skill_root / "agents").mkdir(parents=True)
        (skill_root / "references").mkdir()
        (skill_root / "SKILL.md").write_text(
            f"# {folder}\n\n{label}\n",
            encoding="utf-8",
        )
        (skill_root / "PROVENANCE.md").write_text(
            f"# Provenance\n\n{label}\n",
            encoding="utf-8",
        )
        (skill_root / "agents" / "openai.yaml").write_text(
            f"display_name: {folder}\nlabel: {label}\n",
            encoding="utf-8",
        )
        (skill_root / "references" / "sample.md").write_bytes(
            f"{folder}:{label}\n".encode("utf-8")
        )


def snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if (
            path.is_file()
            and path != root / publish_local.TRANSACTION_LOCK_NAME
        )
    }


class PublishTransactionTests(unittest.TestCase):
    def create_receipt(
        self,
        generated: Path,
        receipt: Path,
        *,
        validated_at: datetime | None = None,
    ) -> dict:
        payload = release_state.write_validation_receipt(
            build_root=generated,
            receipt_path=receipt,
        )
        if validated_at is not None:
            payload["validated_at"] = validated_at.isoformat()
            receipt.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return payload

    def test_missing_receipt_makes_no_destination_changes(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            before = snapshot(destination)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "Validation receipt is missing",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=root / "missing.json",
                )

            self.assertEqual(before, snapshot(destination))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )
    def test_destination_inside_source_is_rejected_without_side_effects(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = generated / "nested" / "skills"
            write_skill_tree(generated, "source")
            before = snapshot(generated)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "must not be equal or contain one another",
            ), patch.object(publish_local, "_stage_all") as stage_all:
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=root / "missing.json",
                )

            stage_all.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertEqual(before, snapshot(generated))
            self.assertEqual(
                [],
                list(generated.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_source_inside_destination_is_rejected_and_preserved(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            destination = root / "skills"
            generated = destination / "generated"
            write_skill_tree(generated, "source")
            keep = destination / "keep.txt"
            keep.write_text("preserve\n", encoding="utf-8")
            before = snapshot(destination)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "must not be equal or contain one another",
            ), patch.object(publish_local, "_stage_all") as stage_all:
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=root / "missing.json",
                )

            stage_all.assert_not_called()
            self.assertEqual(before, snapshot(destination))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_equal_source_and_destination_is_rejected_and_preserved(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            generated = Path(temporary) / "generated"
            write_skill_tree(generated, "source")
            before = snapshot(generated)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "must not be equal or contain one another",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=generated,
                    receipt_path=generated.parent / "missing.json",
                )

            self.assertEqual(before, snapshot(generated))
            self.assertEqual(
                [],
                list(generated.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_in_repo_codex_destination_is_rejected_before_creation(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            fake_repo = root / "repo"
            fake_repo.mkdir()
            generated = root / "generated"
            destination = fake_repo / ".codex" / "skills"
            write_skill_tree(generated, "source")
            stderr = io.StringIO()

            with patch.object(
                publish_local,
                "REPO_ROOT",
                fake_repo,
            ), patch.object(
                publish_local,
                "BUILD_ROOT",
                generated,
            ), patch.object(
                publish_local,
                "_stage_all",
            ) as stage_all, redirect_stderr(stderr):
                exit_code = publish_local.main(["--dest", str(destination)])

            self.assertEqual(1, exit_code)
            self.assertIn("must be outside the repository", stderr.getvalue())
            stage_all.assert_not_called()
            self.assertFalse(destination.exists())
            self.assertFalse(destination.parent.exists())

    def test_case_insensitive_source_alias_with_absent_leaf_is_rejected(
        self,
    ) -> None:
        with TemporaryDirectory(
            prefix=".publish-case-test-",
            dir=REPO_ROOT,
        ) as temporary:
            root = Path(temporary)
            generated = root / "Generated"
            write_skill_tree(generated, "source")
            alias = root / "gENERATED"
            if not alias.exists() or not os.path.samefile(generated, alias):
                self.skipTest("test filesystem is case-sensitive")
            destination = alias / "absent" / "skills"
            before = snapshot(generated)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "must not be equal or contain one another",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=root / "missing.json",
                )

            self.assertFalse(destination.exists())
            self.assertEqual(before, snapshot(generated))
            self.assertFalse(
                (generated / publish_local.TRANSACTION_LOCK_NAME).exists()
            )

    def test_case_insensitive_repo_alias_with_absent_leaf_is_rejected(
        self,
    ) -> None:
        with TemporaryDirectory(
            prefix=".publish-case-test-",
            dir=REPO_ROOT,
        ) as temporary:
            root = Path(temporary)
            fake_repo = root / "Repository"
            fake_repo.mkdir()
            alias = root / "rEPOSITORY"
            if not alias.exists() or not os.path.samefile(fake_repo, alias):
                self.skipTest("test filesystem is case-sensitive")
            generated = root / "generated"
            write_skill_tree(generated, "source")
            destination = alias / ".codex" / "skills"

            with patch.object(
                publish_local,
                "REPO_ROOT",
                fake_repo,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "must be outside the repository",
            ):
                publish_local.preflight_publication_paths(
                    generated,
                    destination,
                )

            self.assertFalse(destination.exists())

    def test_case_insensitive_equal_source_alias_is_rejected_before_lock(
        self,
    ) -> None:
        with TemporaryDirectory(
            prefix=".publish-case-test-",
            dir=REPO_ROOT,
        ) as temporary:
            root = Path(temporary)
            generated = root / "Generated"
            write_skill_tree(generated, "source")
            alias = root / "gENERATED"
            if not alias.exists() or not os.path.samefile(generated, alias):
                self.skipTest("test filesystem is case-sensitive")
            before = snapshot(generated)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "must not be equal or contain one another",
            ), patch.object(
                publish_local,
                "_ensure_destination_root",
            ) as ensure_destination:
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=alias,
                    receipt_path=root / "missing.json",
                )

            ensure_destination.assert_not_called()
            self.assertEqual(before, snapshot(generated))
            self.assertFalse(
                (generated / publish_local.TRANSACTION_LOCK_NAME).exists()
            )

    def test_case_insensitive_destination_alias_containing_source_is_rejected(
        self,
    ) -> None:
        with TemporaryDirectory(
            prefix=".publish-case-test-",
            dir=REPO_ROOT,
        ) as temporary:
            root = Path(temporary)
            container = root / "Container"
            generated = container / "generated"
            write_skill_tree(generated, "source")
            destination_alias = root / "cONTAINER"
            if (
                not destination_alias.exists()
                or not os.path.samefile(container, destination_alias)
            ):
                self.skipTest("test filesystem is case-sensitive")
            before = snapshot(generated)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "must not be equal or contain one another",
            ), patch.object(
                publish_local,
                "_ensure_destination_root",
            ) as ensure_destination:
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination_alias,
                    receipt_path=root / "missing.json",
                )

            ensure_destination.assert_not_called()
            self.assertEqual(before, snapshot(generated))
            self.assertFalse(
                (container / publish_local.TRANSACTION_LOCK_NAME).exists()
            )

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "Linux case-sensitive behavior",
    )
    def test_case_distinct_linux_destination_remains_allowed(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "Generated"
            write_skill_tree(generated, "source")
            destination = root / "gENERATED" / "skills"
            if (root / "gENERATED").exists():
                self.skipTest("test filesystem is not case-sensitive")

            observed_source, observed_destination = (
                publish_local.preflight_publication_paths(
                    generated,
                    destination,
                )
            )

            self.assertEqual(generated.resolve(), observed_source)
            self.assertEqual(destination.resolve(strict=False), observed_destination)

    def test_source_and_destination_root_symlinks_are_rejected(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination_target = root / "destination-target"
            source_link = root / "generated-link"
            destination_link = root / "destination-link"
            write_skill_tree(generated, "source")
            destination_target.mkdir()
            source_link.symlink_to(generated, target_is_directory=True)
            destination_link.symlink_to(
                destination_target,
                target_is_directory=True,
            )
            before_source = snapshot(generated)
            before_destination = snapshot(destination_target)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "source root must not be a symlink",
            ):
                publish_local.publish_skills(
                    source_root=source_link,
                    dest_root=root / "external-skills",
                    receipt_path=root / "missing.json",
                )
            with self.assertRaisesRegex(
                publish_local.PublishError,
                "destination root must not be a symlink",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination_link,
                    receipt_path=root / "missing.json",
                )

            self.assertEqual(before_source, snapshot(generated))
            self.assertEqual(before_destination, snapshot(destination_target))
            self.assertTrue(source_link.is_symlink())
            self.assertTrue(destination_link.is_symlink())

    def test_existing_transaction_lock_aborts_without_touching_destinations(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "concurrent")
            self.create_receipt(generated, receipt)
            lock_path = destination / publish_local.TRANSACTION_LOCK_NAME
            lock_path.write_text("concurrent lock\n", encoding="utf-8")
            before = snapshot(destination)
            root_lock_descriptor = os.open(destination, os.O_RDONLY)
            fcntl.flock(
                root_lock_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            try:
                with patch.object(
                    publish_local,
                    "_atomic_rename_no_replace",
                    side_effect=AssertionError("destination swap attempted"),
                ), self.assertRaisesRegex(
                    publish_local.PublishError,
                    "publication transaction is active",
                ):
                    publish_local.publish_skills(
                        source_root=generated,
                        dest_root=destination,
                        receipt_path=receipt,
                    )
            finally:
                fcntl.flock(root_lock_descriptor, fcntl.LOCK_UN)
                os.close(root_lock_descriptor)

            self.assertEqual(before, snapshot(destination))
            self.assertEqual("concurrent lock\n", lock_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_unresolved_recovery_transaction_blocks_publication(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            recovery = (
                destination
                / f"{publish_local.TRANSACTION_PREFIX}manual-recovery"
            )
            recovery.mkdir()
            marker = recovery / "README.txt"
            marker.write_text("manual recovery required\n", encoding="utf-8")
            before = snapshot(destination)

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=AssertionError("destination swap attempted"),
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "recovery transaction blocks publication",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(before, snapshot(destination))
            self.assertEqual(
                "manual recovery required\n",
                marker.read_text(encoding="utf-8"),
            )
            self.assertTrue(
                (destination / publish_local.TRANSACTION_LOCK_NAME).is_file()
            )

    def test_hard_linked_sentinel_is_rejected_without_mutating_victim(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            victim = root / "victim.txt"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            victim.write_text("owner bytes\n", encoding="utf-8")
            sentinel = destination / publish_local.TRANSACTION_LOCK_NAME
            os.link(victim, sentinel)
            before = snapshot(destination)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "singly linked regular file",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual("owner bytes\n", victim.read_text(encoding="utf-8"))
            self.assertEqual("owner bytes\n", sentinel.read_text(encoding="utf-8"))
            self.assertEqual(before, snapshot(destination))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_fifo_sentinel_is_rejected_without_blocking(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            sentinel = destination / publish_local.TRANSACTION_LOCK_NAME
            os.mkfifo(sentinel)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "singly linked regular file",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(stat.S_ISFIFO(sentinel.lstat().st_mode))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_destination_root_symlink_substitution_before_lock_is_rejected(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            moved_destination = root / "moved-skills"
            unintended_target = root / "unintended"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            unintended_target.mkdir()
            marker = unintended_target / "owner.txt"
            marker.write_text("concurrent owner\n", encoding="utf-8")
            self.create_receipt(generated, receipt)
            real_acquire = publish_local._acquire_transaction_lock

            def substitute_root_then_acquire(
                identity: publish_local.DestinationRootIdentity,
            ) -> publish_local.TransactionLock:
                destination.rename(moved_destination)
                destination.symlink_to(
                    unintended_target,
                    target_is_directory=True,
                )
                return real_acquire(identity)

            with patch.object(
                publish_local,
                "_acquire_transaction_lock",
                side_effect=substitute_root_then_acquire,
            ), patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=AssertionError("destination swap attempted"),
            ), self.assertRaises(
                publish_local.PublishError,
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(destination.is_symlink())
            self.assertEqual(unintended_target.resolve(), destination.resolve())
            self.assertEqual("concurrent owner\n", marker.read_text(encoding="utf-8"))
            self.assertTrue(moved_destination.is_dir())
            self.assertFalse(
                (unintended_target / publish_local.TRANSACTION_LOCK_NAME).exists()
            )
            self.assertEqual(
                [],
                list(
                    unintended_target.glob(
                        f"{publish_local.TRANSACTION_PREFIX}*"
                    )
                ),
            )

    def test_absent_destination_ancestor_swap_cannot_create_in_attacker_tree(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            parent = root / "destination-parent"
            moved_parent = root / "moved-destination-parent"
            attacker = root / "attacker"
            destination = parent / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            parent.mkdir()
            attacker.mkdir()
            self.create_receipt(generated, receipt)
            real_mkdir = os.mkdir
            substituted = False

            def substitute_parent_before_leaf_creation(
                name: str | bytes,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> None:
                nonlocal substituted
                if name == "skills" and dir_fd is not None and not substituted:
                    substituted = True
                    parent.rename(moved_parent)
                    parent.symlink_to(attacker, target_is_directory=True)
                real_mkdir(name, mode, dir_fd=dir_fd)

            with patch.object(
                publish_local.os,
                "mkdir",
                side_effect=substitute_parent_before_leaf_creation,
            ), self.assertRaises(publish_local.PublishError):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(substituted)
            self.assertTrue(parent.is_symlink())
            self.assertFalse((attacker / "skills").exists())
            self.assertTrue((moved_parent / "skills").is_dir())

    def test_old_receipt_is_rejected_before_staging(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            now = datetime.now(timezone.utc)
            self.create_receipt(
                generated,
                receipt,
                validated_at=now - publish_local.RECEIPT_MAX_AGE - timedelta(seconds=1),
            )
            before = snapshot(destination)

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "older than one hour",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                    now=now,
                )

            self.assertEqual(before, snapshot(destination))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_tree_change_after_validation_is_rejected(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "validated")
            self.create_receipt(generated, receipt)
            changed_file = generated / "stata-core" / "SKILL.md"
            changed_file.write_text("# Changed after validation\n", encoding="utf-8")

            with self.assertRaisesRegex(
                publish_local.PublishError,
                "stale for build/generated",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertFalse(destination.exists())

    def test_default_destination_honors_codex_home(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            receipt = generated.parent / "validation-receipt.json"
            codex_home = root / "custom-codex-home"
            write_skill_tree(generated, "new")
            self.create_receipt(generated, receipt)

            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}), patch.object(
                publish_local,
                "BUILD_ROOT",
                generated,
            ):
                exit_code = publish_local.main([])

            self.assertEqual(0, exit_code)
            self.assertEqual(
                snapshot(generated),
                snapshot(codex_home / "skills"),
            )
            self.assertEqual(
                [],
                list(
                    (codex_home / "skills").glob(
                        f"{publish_local.TRANSACTION_PREFIX}*"
                    )
                ),
            )

    def test_external_custom_destination_succeeds_via_cli(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "external-skills"
            receipt = generated.parent / "validation-receipt.json"
            write_skill_tree(generated, "external")
            self.create_receipt(generated, receipt)
            stderr = io.StringIO()

            with patch.object(
                publish_local,
                "BUILD_ROOT",
                generated,
            ), redirect_stderr(stderr):
                exit_code = publish_local.main(["--dest", str(destination)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())
            self.assertEqual(snapshot(generated), snapshot(destination))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_copy_failure_is_reported_and_preserves_transaction(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "external-skills"
            receipt = generated.parent / "validation-receipt.json"
            write_skill_tree(generated, "external")
            self.create_receipt(generated, receipt)
            stderr = io.StringIO()

            with patch.object(
                publish_local,
                "BUILD_ROOT",
                generated,
            ), patch.object(
                publish_local,
                "_stage_all",
                side_effect=shutil.Error("forced copy failure"),
            ), redirect_stderr(
                stderr,
            ):
                exit_code = publish_local.main(["--dest", str(destination)])

            self.assertEqual(1, exit_code)
            self.assertIn("ERROR: Publication staging failed", stderr.getvalue())
            self.assertIn(
                "Transaction recovery state was preserved for review",
                stderr.getvalue(),
            )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_staging_failure_preserves_substituted_stage_foreign_bytes(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "external-skills"
            receipt = root / "validation-receipt.json"
            displaced_stage = root / "displaced-stage"
            write_skill_tree(generated, "external")
            self.create_receipt(generated, receipt)
            real_stage_all = publish_local._stage_all
            foreign_marker: Path | None = None

            def substitute_stage_after_copy(
                source_root: Path,
                transaction_root: publish_local.DirectoryHandle,
            ) -> publish_local.DirectoryHandle:
                nonlocal foreign_marker
                stage_root = real_stage_all(source_root, transaction_root)
                stage_root.display_path.rename(displaced_stage)
                replacement_stage = stage_root.display_path
                replacement_stage.mkdir()
                foreign_marker = replacement_stage / "foreign-owner-bytes.txt"
                foreign_marker.write_text(
                    "preserve foreign staging bytes\n",
                    encoding="utf-8",
                )
                (
                    displaced_stage / "stata-core" / "SKILL.md"
                ).write_text(
                    "# Concurrent staged bytes\n",
                    encoding="utf-8",
                )
                return stage_root

            with patch.object(
                publish_local,
                "_stage_all",
                side_effect=substitute_stage_after_copy,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "Transaction recovery state was preserved for review",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertIsNotNone(foreign_marker)
            assert foreign_marker is not None
            self.assertEqual(
                "preserve foreign staging bytes\n",
                foreign_marker.read_text(encoding="utf-8"),
            )
            transactions = list(
                destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")
            )
            self.assertEqual(1, len(transactions))
            self.assertTrue(
                foreign_marker.resolve().is_relative_to(
                    transactions[0].resolve()
                )
            )
            self.assertTrue(displaced_stage.is_dir())
            for folder in release_state.SKILL_FOLDERS:
                self.assertFalse((destination / folder).exists())

    def test_receipt_cli_override_cannot_read_repository_artifacts(self) -> None:
        readme = REPO_ROOT / "README.md"
        before = readme.read_bytes()
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            publish_local.main(["--receipt", str(readme)])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments", stderr.getvalue())
        self.assertEqual(before, readme.read_bytes())

    def test_successful_publish_is_byte_identical_and_cleans_transaction(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new bytes")
            write_skill_tree(destination, "old bytes")
            unrelated = destination / "unrelated-skill" / "keep.txt"
            unrelated.parent.mkdir()
            unrelated.write_text("leave me alone\n", encoding="utf-8")
            payload = self.create_receipt(generated, receipt)

            observed = publish_local.publish_skills(
                source_root=generated,
                dest_root=destination,
                receipt_path=receipt,
            )

            self.assertEqual(payload, observed)
            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(
                    snapshot(generated / folder),
                    snapshot(destination / folder),
                )
            self.assertEqual("leave me alone\n", unrelated.read_text(encoding="utf-8"))
            self.assertTrue(
                (destination / publish_local.TRANSACTION_LOCK_NAME).is_file()
            )
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )
            self.assertEqual(
                [],
                list(
                    destination.glob(
                        f"{publish_local.CLEANUP_QUARANTINE_PREFIX}*"
                    )
                ),
            )

    def test_persistent_sentinel_allows_later_publication(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "first")
            self.create_receipt(generated, receipt)
            publish_local.publish_skills(
                source_root=generated,
                dest_root=destination,
                receipt_path=receipt,
            )

            shutil.rmtree(generated)
            write_skill_tree(generated, "second")
            self.create_receipt(generated, receipt)
            publish_local.publish_skills(
                source_root=generated,
                dest_root=destination,
                receipt_path=receipt,
            )

            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(
                    snapshot(generated / folder),
                    snapshot(destination / folder),
                )
            self.assertTrue(
                (destination / publish_local.TRANSACTION_LOCK_NAME).is_file()
            )
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_mid_swap_failure_rolls_back_and_preserves_transaction(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            before = {
                folder: snapshot(destination / folder)
                for folder in release_state.SKILL_FOLDERS
            }
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0

            def fail_second_install(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 4:
                    raise OSError("forced second install failure")
                real_replace(source, target, **kwargs)

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=fail_second_install,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "all destinations were rolled back",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(before[folder], snapshot(destination / folder))
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_atomic_no_replace_preserves_existing_empty_directory(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            source_parent = root / "source"
            destination_parent = root / "destination"
            source_parent.mkdir()
            destination_parent.mkdir()
            source = source_parent / "skill"
            destination = destination_parent / "skill"
            source.mkdir()
            destination.mkdir()
            marker = source / "source.txt"
            marker.write_text("source bytes\n", encoding="utf-8")
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            source_descriptor = os.open(source_parent, flags)
            destination_descriptor = os.open(destination_parent, flags)
            try:
                with self.assertRaises(OSError) as raised:
                    publish_local._atomic_rename_no_replace(
                        "skill",
                        "skill",
                        src_dir_fd=source_descriptor,
                        dst_dir_fd=destination_descriptor,
                    )
            finally:
                os.close(destination_descriptor)
                os.close(source_descriptor)

            self.assertIn(raised.exception.errno, {errno.EEXIST, errno.ENOTEMPTY})
            self.assertEqual("source bytes\n", marker.read_text(encoding="utf-8"))
            self.assertTrue(destination.is_dir())
            self.assertEqual([], list(destination.iterdir()))

    def test_last_moment_install_directory_is_never_overwritten(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            real_rename = publish_local._atomic_rename_no_replace
            rename_calls = 0
            concurrent_destination = destination / "stata-core"
            owner_marker = concurrent_destination / "owner.txt"

            def inject_before_first_install(
                source: str | bytes,
                target: str | bytes,
                **kwargs: int,
            ) -> None:
                nonlocal rename_calls
                rename_calls += 1
                if rename_calls == 2:
                    concurrent_destination.mkdir()
                    owner_marker.write_text(
                        "concurrent owner bytes\n",
                        encoding="utf-8",
                    )
                real_rename(source, target, **kwargs)

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=inject_before_first_install,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "rollback was incomplete",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(2, rename_calls)
            self.assertEqual(
                "concurrent owner bytes\n",
                owner_marker.read_text(encoding="utf-8"),
            )
            transactions = list(
                destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")
            )
            self.assertEqual(1, len(transactions))
            self.assertIn(
                "old",
                (
                    transactions[0]
                    / "backups"
                    / "stata-core"
                    / "SKILL.md"
                ).read_text(encoding="utf-8"),
            )

    def test_backup_fsync_failure_restores_original_and_preserves_recovery(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            before = {
                folder: snapshot(destination / folder)
                for folder in release_state.SKILL_FOLDERS
            }
            real_fsync_directory = publish_local._fsync_directory_descriptor
            failed = False

            def fail_first_backup_fsync(
                file_descriptor: int,
                display_path: Path,
                *,
                preserve_transaction: bool = False,
            ) -> None:
                nonlocal failed
                if display_path.name == "backups" and not failed:
                    failed = True
                    raise publish_local.PublishError(
                        "forced backup directory fsync failure",
                        preserve_transaction=True,
                    )
                real_fsync_directory(
                    file_descriptor,
                    display_path,
                    preserve_transaction=preserve_transaction,
                )

            with patch.object(
                publish_local,
                "_fsync_directory_descriptor",
                side_effect=fail_first_backup_fsync,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "destinations were restored",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(failed)
            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(before[folder], snapshot(destination / folder))
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_rollback_setup_descriptor_failure_preserves_only_backup(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            expected_core = snapshot(destination / "stata-core")
            real_replace = publish_local._atomic_rename_no_replace
            real_rollback = publish_local._rollback
            replace_calls = 0

            def fail_first_install(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    raise OSError(
                        "forced install failure before rollback setup"
                    )
                real_replace(source, target, **kwargs)

            def close_backup_before_rollback(
                states: list[publish_local.ReplacementState],
                destination_root: publish_local.DestinationRootIdentity,
                transaction_lock: publish_local.TransactionLock,
                backup_root: publish_local.DirectoryHandle,
                quarantine_root: publish_local.DirectoryHandle,
            ) -> list[str]:
                assert backup_root.file_descriptor is not None
                os.close(backup_root.file_descriptor)
                backup_root.file_descriptor = None
                return real_rollback(
                    states,
                    destination_root,
                    transaction_lock,
                    backup_root,
                    quarantine_root,
                )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=fail_first_install,
            ), patch.object(
                publish_local,
                "_rollback",
                side_effect=close_backup_before_rollback,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "rollback was incomplete",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertFalse((destination / "stata-core").exists())
            transactions = list(
                destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")
            )
            self.assertEqual(1, len(transactions))
            self.assertEqual(
                expected_core,
                snapshot(
                    transactions[0] / "backups" / "stata-core"
                ),
            )

    def test_concurrent_destination_bytes_survive_without_any_swap(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            concurrent_file = destination / "stata-core" / "SKILL.md"
            real_stage_all = publish_local._stage_all
            real_replace = publish_local._atomic_rename_no_replace

            def stage_then_mutate(
                source_root: Path,
                transaction_root: Path,
            ) -> Path:
                stage_root = real_stage_all(source_root, transaction_root)
                concurrent_file.write_text(
                    "# Concurrent owner bytes\n",
                    encoding="utf-8",
                )
                return stage_root

            def reject_destination_swap(
                source: str | bytes,
                target: str | bytes,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                if (
                    os.fsdecode(source) in release_state.SKILL_FOLDERS
                    and os.fsdecode(target) in release_state.SKILL_FOLDERS
                ):
                    raise AssertionError("destination swap attempted")
                real_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with patch.object(
                publish_local,
                "_stage_all",
                side_effect=stage_then_mutate,
            ), patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=reject_destination_swap,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "changed after publication preflight",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(
                "# Concurrent owner bytes\n",
                concurrent_file.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "old",
                (destination / "stata-packages" / "SKILL.md").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue(
                (destination / publish_local.TRANSACTION_LOCK_NAME).exists()
            )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_concurrent_destination_symlink_survives_without_any_swap(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            moved_destination = root / "concurrent-original"
            symlink_target = root / "concurrent-target"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            symlink_target.mkdir()
            target_file = symlink_target / "owner.txt"
            target_file.write_text("concurrent owner\n", encoding="utf-8")
            self.create_receipt(generated, receipt)
            real_stage_all = publish_local._stage_all
            real_replace = publish_local._atomic_rename_no_replace
            replaced_destination = destination / "stata-packages"

            def stage_then_replace_with_symlink(
                source_root: Path,
                transaction_root: Path,
            ) -> Path:
                stage_root = real_stage_all(source_root, transaction_root)
                replaced_destination.rename(moved_destination)
                replaced_destination.symlink_to(
                    symlink_target,
                    target_is_directory=True,
                )
                return stage_root

            def reject_destination_swap(
                source: str | bytes,
                target: str | bytes,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                if (
                    os.fsdecode(source) in release_state.SKILL_FOLDERS
                    and os.fsdecode(target) in release_state.SKILL_FOLDERS
                ):
                    raise AssertionError("destination swap attempted")
                real_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with patch.object(
                publish_local,
                "_stage_all",
                side_effect=stage_then_replace_with_symlink,
            ), patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=reject_destination_swap,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "symlinked skill destination",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(replaced_destination.is_symlink())
            self.assertEqual(
                symlink_target.resolve(),
                replaced_destination.resolve(),
            )
            self.assertEqual("concurrent owner\n", target_file.read_text(encoding="utf-8"))
            self.assertTrue(moved_destination.is_dir())
            self.assertTrue(
                (destination / publish_local.TRANSACTION_LOCK_NAME).exists()
            )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_sentinel_replacement_after_authorization_keeps_root_lock(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0

            def replace_sentinel_before_first_swap(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                if replace_calls == 0:
                    lock_path = destination / publish_local.TRANSACTION_LOCK_NAME
                    lock_path.unlink()
                    lock_path.write_text(
                        "concurrent sentinel\n",
                        encoding="utf-8",
                    )
                    probe_descriptor = os.open(destination, os.O_RDONLY)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(
                                probe_descriptor,
                                fcntl.LOCK_EX | fcntl.LOCK_NB,
                            )
                    finally:
                        os.close(probe_descriptor)
                replace_calls += 1
                real_replace(source, target, **kwargs)

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=replace_sentinel_before_first_swap,
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            lock_path = destination / publish_local.TRANSACTION_LOCK_NAME
            self.assertEqual(
                "concurrent sentinel\n",
                lock_path.read_text(encoding="utf-8"),
            )
            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(
                    snapshot(generated / folder),
                    snapshot(destination / folder),
                )
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_root_substitution_during_swap_prevents_rollback_mutations(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            moved_destination = root / "moved-skills"
            unintended_target = root / "unintended"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            write_skill_tree(unintended_target, "attacker")
            marker = unintended_target / "owner.txt"
            marker.write_text("concurrent owner\n", encoding="utf-8")
            unintended_before = snapshot(unintended_target)
            self.create_receipt(generated, receipt)
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0

            def substitute_root_before_first_replace(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    destination.rename(moved_destination)
                    destination.symlink_to(
                        unintended_target,
                        target_is_directory=True,
                    )
                real_replace(source, target, **kwargs)

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=substitute_root_before_first_replace,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "rollback was incomplete",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(1, replace_calls)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(unintended_target.resolve(), destination.resolve())
            self.assertEqual("concurrent owner\n", marker.read_text(encoding="utf-8"))
            self.assertEqual(unintended_before, snapshot(unintended_target))
            self.assertFalse(
                (unintended_target / publish_local.TRANSACTION_LOCK_NAME).exists()
            )
            self.assertTrue(
                (moved_destination / publish_local.TRANSACTION_LOCK_NAME).is_file()
            )
            self.assertEqual(
                1,
                len(
                    list(
                        moved_destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_edit_between_precheck_and_backup_is_restored_not_deleted(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            concurrent_file = destination / "stata-core" / "SKILL.md"
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0

            def edit_before_first_backup(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    concurrent_file.write_text(
                        "# Concurrent pre-backup bytes\n",
                        encoding="utf-8",
                    )
                real_replace(source, target, **kwargs)

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=edit_before_first_backup,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "all destinations were rolled back",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(2, replace_calls)
            self.assertEqual(
                "# Concurrent pre-backup bytes\n",
                concurrent_file.read_text(encoding="utf-8"),
            )
            for folder in ("stata-packages", "stata-c-plugins"):
                self.assertIn(
                    "old",
                    (destination / folder / "SKILL.md").read_text(
                        encoding="utf-8"
                    ),
                )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_later_child_change_after_first_swap_survives_clean_rollback(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            concurrent_file = destination / "stata-packages" / "SKILL.md"
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0

            def mutate_later_child_after_first_install(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                real_replace(source, target, **kwargs)
                if replace_calls == 2:
                    concurrent_file.write_text(
                        "# Concurrent package bytes\n",
                        encoding="utf-8",
                    )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=mutate_later_child_after_first_install,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "all destinations were rolled back",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(4, replace_calls)
            self.assertEqual(
                "# Concurrent package bytes\n",
                concurrent_file.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "old",
                (destination / "stata-core" / "SKILL.md").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_installed_child_change_before_rollback_is_never_removed(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            concurrent_file = destination / "stata-core" / "SKILL.md"
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0

            def mutate_first_installed_child_after_all_installs(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                real_replace(source, target, **kwargs)
                if replace_calls == 6:
                    concurrent_file.write_text(
                        "# Concurrent installed bytes\n",
                        encoding="utf-8",
                    )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=mutate_first_installed_child_after_all_installs,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "rollback was incomplete",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(10, replace_calls)
            self.assertEqual(
                "# Concurrent installed bytes\n",
                concurrent_file.read_text(encoding="utf-8"),
            )
            for folder in ("stata-packages", "stata-c-plugins"):
                self.assertIn(
                    "old",
                    (destination / folder / "SKILL.md").read_text(
                        encoding="utf-8"
                    ),
                )
            self.assertTrue(
                (destination / publish_local.TRANSACTION_LOCK_NAME).is_file()
            )
            transactions = list(
                destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")
            )
            self.assertEqual(1, len(transactions))
            self.assertIn(
                "old",
                (
                    transactions[0]
                    / "backups"
                    / "stata-core"
                    / "SKILL.md"
                ).read_text(encoding="utf-8"),
            )

    def test_post_install_digest_failure_rolls_back_every_skill(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            before = {
                folder: snapshot(destination / folder)
                for folder in release_state.SKILL_FOLDERS
            }
            real_skill_digest = publish_local._skill_digest_at
            package_digest_calls = 0

            def corrupt_installed_digest(
                parent_descriptor: int,
                name: str,
                display_path: Path,
            ) -> str:
                nonlocal package_digest_calls
                if name == "stata-packages":
                    package_digest_calls += 1
                    if package_digest_calls == 2:
                        return "0" * 64
                return real_skill_digest(
                    parent_descriptor,
                    name,
                    display_path,
                )

            with patch.object(
                publish_local,
                "_skill_digest_at",
                side_effect=corrupt_installed_digest,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "all destinations were rolled back",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(before[folder], snapshot(destination / folder))
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_foreign_quarantine_content_is_preserved_after_abort(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0
            foreign_marker: Path | None = None

            def inject_foreign_quarantine_content(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal foreign_marker, replace_calls
                real_replace(source, target, **kwargs)
                replace_calls += 1
                if replace_calls == 1:
                    transaction = next(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                    foreign_root = (
                        transaction / "quarantine" / "stata-core"
                    )
                    foreign_root.mkdir()
                    foreign_marker = foreign_root / "owner.txt"
                    foreign_marker.write_text(
                        "preserve concurrent quarantine bytes\n",
                        encoding="utf-8",
                    )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=inject_foreign_quarantine_content,
            ), self.assertRaises(publish_local.PublishError) as raised:
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertIn("rollback was incomplete", str(raised.exception))
            self.assertIsNotNone(foreign_marker)
            assert foreign_marker is not None
            self.assertEqual(
                "preserve concurrent quarantine bytes\n",
                foreign_marker.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_foreign_content_added_after_clean_rollback_is_preserved(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            before = {
                folder: snapshot(destination / folder)
                for folder in release_state.SKILL_FOLDERS
            }
            real_replace = publish_local._atomic_rename_no_replace
            real_rollback = publish_local._rollback
            replace_calls = 0
            foreign_marker: Path | None = None

            def fail_second_install(
                source: str | bytes,
                target: str | bytes,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 4:
                    raise OSError("forced second install failure")
                real_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            def rollback_then_add_foreign_content(
                states: list[publish_local.ReplacementState],
                destination_root: publish_local.DestinationRootIdentity,
                transaction_lock: publish_local.TransactionLock,
                backup_root: publish_local.DirectoryHandle,
                quarantine_root: publish_local.DirectoryHandle,
            ) -> list[str]:
                nonlocal foreign_marker
                errors = real_rollback(
                    states,
                    destination_root,
                    transaction_lock,
                    backup_root,
                    quarantine_root,
                )
                foreign_marker = (
                    backup_root.display_path.parent
                    / "foreign-after-rollback.txt"
                )
                foreign_marker.write_text(
                    "preserve post-rollback bytes\n",
                    encoding="utf-8",
                )
                return errors

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=fail_second_install,
            ), patch.object(
                publish_local,
                "_rollback",
                side_effect=rollback_then_add_foreign_content,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "all destinations were rolled back",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertIsNotNone(foreign_marker)
            assert foreign_marker is not None
            self.assertEqual(
                "preserve post-rollback bytes\n",
                foreign_marker.read_text(encoding="utf-8"),
            )
            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(before[folder], snapshot(destination / folder))
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_committed_cleanup_preserves_unrecognized_transaction_content(
        self,
    ) -> None:
        relative_markers = (
            Path("foreign-root.txt"),
            Path("stage") / "foreign-stage.txt",
            Path("quarantine") / "foreign-quarantine.txt",
            Path("backups") / "stata-core" / "foreign-backup.txt",
        )
        for relative_marker in relative_markers:
            with self.subTest(relative_marker=relative_marker), TemporaryDirectory(
                prefix="publish-test-"
            ) as temporary:
                root = Path(temporary)
                generated = root / "generated"
                destination = root / "skills"
                receipt = root / "receipt.json"
                write_skill_tree(generated, "new")
                write_skill_tree(destination, "old")
                self.create_receipt(generated, receipt)
                real_verify = publish_local._verify_success_cleanup_state
                foreign_marker: Path | None = None

                def verify_then_inject(
                    transaction_root: publish_local.DirectoryHandle,
                    cleanup_plan: publish_local.TransactionCleanupPlan,
                ) -> None:
                    nonlocal foreign_marker
                    real_verify(transaction_root, cleanup_plan)
                    foreign_marker = (
                        transaction_root.display_path / relative_marker
                    )
                    foreign_marker.write_text(
                        "preserve foreign transaction bytes\n",
                        encoding="utf-8",
                    )

                with patch.object(
                    publish_local,
                    "_verify_success_cleanup_state",
                    side_effect=verify_then_inject,
                ), self.assertRaisesRegex(
                    publish_local.PublishError,
                    "committed and verified.*unrecognized state",
                ):
                    publish_local.publish_skills(
                        source_root=generated,
                        dest_root=destination,
                        receipt_path=receipt,
                    )

                self.assertIsNotNone(foreign_marker)
                assert foreign_marker is not None
                self.assertEqual(
                    "preserve foreign transaction bytes\n",
                    foreign_marker.read_text(encoding="utf-8"),
                )
                self.assertEqual(
                    1,
                    len(
                        list(
                            destination.glob(
                                f"{publish_local.TRANSACTION_PREFIX}*"
                            )
                        )
                    ),
                )
                for folder in release_state.SKILL_FOLDERS:
                    self.assertEqual(
                        snapshot(generated / folder),
                        snapshot(destination / folder),
                    )

    def test_committed_cleanup_preserves_backup_entry_added_after_manifest(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            real_clear = publish_local._clear_directory_handle
            foreign_marker: Path | None = None

            def inject_after_cleanup_manifest(
                handle: publish_local.DirectoryHandle,
                **kwargs: object,
            ) -> None:
                nonlocal foreign_marker
                if (
                    foreign_marker is None
                    and kwargs.get("expected_records") is not None
                ):
                    foreign_marker = handle.display_path / "foreign-race.txt"
                    foreign_marker.write_text(
                        "preserve late transaction bytes\n",
                        encoding="utf-8",
                    )
                real_clear(handle, **kwargs)

            with patch.object(
                publish_local,
                "_clear_directory_handle",
                side_effect=inject_after_cleanup_manifest,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "committed and verified.*unrecognized state",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertIsNotNone(foreign_marker)
            assert foreign_marker is not None
            self.assertEqual(
                "preserve late transaction bytes\n",
                foreign_marker.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )
            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(
                    snapshot(generated / folder),
                    snapshot(destination / folder),
                )

    def test_committed_cleanup_preserves_per_file_replacement_before_move(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            real_replace = publish_local._atomic_rename_no_replace
            injected = False
            displaced_name: str | None = None
            expected_payload: bytes | None = None
            foreign_payload = b"preserve per-file replacement bytes\n"

            def replace_source_before_cleanup_move(
                source: str | bytes,
                target: str | bytes,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                nonlocal displaced_name, expected_payload, injected
                target_name = os.fsdecode(target)
                if (
                    not injected
                    and target_name.startswith(
                        publish_local.CLEANUP_FILE_PREFIX
                    )
                ):
                    source_name = os.fsdecode(source)
                    displaced_name = f"displaced-{source_name}"
                    source_descriptor = os.open(
                        source,
                        os.O_RDONLY,
                        dir_fd=src_dir_fd,
                    )
                    try:
                        expected_payload = os.read(
                            source_descriptor,
                            1024 * 1024,
                        )
                    finally:
                        os.close(source_descriptor)
                    os.rename(
                        source,
                        displaced_name,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=src_dir_fd,
                    )
                    replacement_descriptor = os.open(
                        source,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                        dir_fd=src_dir_fd,
                    )
                    try:
                        os.write(
                            replacement_descriptor,
                            foreign_payload,
                        )
                    finally:
                        os.close(replacement_descriptor)
                    injected = True
                real_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=replace_source_before_cleanup_move,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "committed and verified.*unrecognized state",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(injected)
            self.assertIsNotNone(displaced_name)
            self.assertIsNotNone(expected_payload)
            assert displaced_name is not None
            assert expected_payload is not None
            displaced_paths = list(destination.rglob(displaced_name))
            self.assertEqual(1, len(displaced_paths))
            self.assertEqual(
                expected_payload,
                displaced_paths[0].read_bytes(),
            )
            restored_name = displaced_name.removeprefix("displaced-")
            self.assertEqual(
                foreign_payload,
                (displaced_paths[0].parent / restored_name).read_bytes(),
            )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_committed_cleanup_rechecks_file_immediately_before_unlink(
        self,
    ) -> None:
        for mode in ("replacement", "same-inode-mutation"):
            with self.subTest(mode=mode), TemporaryDirectory(
                prefix="publish-test-"
            ) as temporary:
                root = Path(temporary)
                generated = root / "generated"
                destination = root / "skills"
                receipt = root / "receipt.json"
                write_skill_tree(generated, "new")
                write_skill_tree(destination, "old")
                self.create_receipt(generated, receipt)
                real_read = publish_local._read_regular_file_at
                cleanup_reads = 0
                injected_payload = (
                    b"preserve post-read replacement bytes\n"
                    if mode == "replacement"
                    else b"preserve post-read appended bytes\n"
                )
                accepted_payload: bytes | None = None

                def mutate_after_cleanup_read(
                    parent_descriptor: int,
                    name: str,
                    display_path: Path,
                    expected: os.stat_result,
                ) -> bytes:
                    nonlocal accepted_payload, cleanup_reads
                    payload = real_read(
                        parent_descriptor,
                        name,
                        display_path,
                        expected,
                    )
                    if (
                        display_path.parent.name.startswith(
                            publish_local.CLEANUP_QUARANTINE_PREFIX
                        )
                        and display_path.name.startswith(
                            publish_local.CLEANUP_FILE_PREFIX
                        )
                    ):
                        cleanup_reads += 1
                        accepted_payload = payload
                        if mode == "replacement" and cleanup_reads == 1:
                            os.rename(
                                name,
                                f"displaced-{name}",
                                src_dir_fd=parent_descriptor,
                                dst_dir_fd=parent_descriptor,
                            )
                            replacement_descriptor = os.open(
                                name,
                                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                                0o600,
                                dir_fd=parent_descriptor,
                            )
                            try:
                                os.write(
                                    replacement_descriptor,
                                    injected_payload,
                                )
                            finally:
                                os.close(replacement_descriptor)
                        elif (
                            mode == "same-inode-mutation"
                            and cleanup_reads == 2
                        ):
                            mutation_descriptor = os.open(
                                name,
                                os.O_WRONLY | os.O_APPEND,
                                dir_fd=parent_descriptor,
                            )
                            try:
                                os.write(
                                    mutation_descriptor,
                                    injected_payload,
                                )
                            finally:
                                os.close(mutation_descriptor)
                    return payload

                with patch.object(
                    publish_local,
                    "_read_regular_file_at",
                    side_effect=mutate_after_cleanup_read,
                ), self.assertRaisesRegex(
                    publish_local.PublishError,
                    "committed and verified.*unrecognized state",
                ):
                    publish_local.publish_skills(
                        source_root=generated,
                        dest_root=destination,
                        receipt_path=receipt,
                    )

                self.assertIsNotNone(accepted_payload)
                assert accepted_payload is not None
                transactions = list(
                    destination.glob(
                        f"{publish_local.TRANSACTION_PREFIX}*"
                    )
                )
                self.assertEqual(1, len(transactions))
                retained_payloads = [
                    path.read_bytes()
                    for path in transactions[0].rglob("*")
                    if path.is_file()
                ]
                if mode == "replacement":
                    self.assertIn(injected_payload, retained_payloads)
                    self.assertIn(accepted_payload, retained_payloads)
                else:
                    self.assertIn(
                        accepted_payload + injected_payload,
                        retained_payloads,
                    )

    def test_committed_cleanup_preserves_empty_directory_replacement(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            expected_core = snapshot(destination / "stata-core")
            real_replace = publish_local._atomic_rename_no_replace
            injected = False
            displaced_name: str | None = None
            restored_name: str | None = None

            def replace_directory_before_cleanup_move(
                source: str | bytes,
                target: str | bytes,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                nonlocal displaced_name, injected, restored_name
                target_name = os.fsdecode(target)
                if (
                    not injected
                    and os.fsdecode(source) == "stata-core"
                    and target_name.startswith(
                        publish_local.CLEANUP_DIRECTORY_PREFIX
                    )
                ):
                    restored_name = os.fsdecode(source)
                    displaced_name = f"displaced-{restored_name}"
                    os.rename(
                        source,
                        displaced_name,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=src_dir_fd,
                    )
                    os.mkdir(
                        source,
                        0o700,
                        dir_fd=src_dir_fd,
                    )
                    injected = True
                real_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=replace_directory_before_cleanup_move,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "committed and verified.*unrecognized state",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(injected)
            self.assertIsNotNone(displaced_name)
            self.assertIsNotNone(restored_name)
            assert displaced_name is not None
            assert restored_name is not None
            transactions = list(
                destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")
            )
            self.assertEqual(1, len(transactions))
            backup_root = transactions[0] / "backups"
            self.assertEqual(
                expected_core,
                snapshot(backup_root / displaced_name),
            )
            self.assertTrue((backup_root / restored_name).is_dir())
            self.assertEqual(
                [],
                list((backup_root / restored_name).iterdir()),
            )

    def test_public_empty_cleanup_directories_are_quarantined_before_rmdir(
        self,
    ) -> None:
        for selected in (
            "stage",
            "backups",
            "quarantine",
            "transaction-root",
        ):
            with self.subTest(selected=selected), TemporaryDirectory(
                prefix="publish-test-"
            ) as temporary:
                root = Path(temporary)
                generated = root / "generated"
                destination = root / "skills"
                receipt = root / "receipt.json"
                write_skill_tree(generated, "new")
                write_skill_tree(destination, "old")
                self.create_receipt(generated, receipt)
                real_replace = publish_local._atomic_rename_no_replace
                displaced_name: str | None = None
                restored_name: str | None = None
                original_identity: tuple[int, int] | None = None
                replacement_identity: tuple[int, int] | None = None

                def substitute_public_empty_directory(
                    source: str | bytes,
                    target: str | bytes,
                    *,
                    src_dir_fd: int,
                    dst_dir_fd: int,
                ) -> None:
                    nonlocal displaced_name
                    nonlocal original_identity
                    nonlocal replacement_identity
                    nonlocal restored_name
                    source_name = os.fsdecode(source)
                    target_name = os.fsdecode(target)
                    selected_source = (
                        source_name == selected
                        or (
                            selected == "transaction-root"
                            and source_name.startswith(
                                publish_local.TRANSACTION_PREFIX
                            )
                        )
                    )
                    if (
                        displaced_name is None
                        and selected_source
                        and target_name.startswith(
                            publish_local.CLEANUP_DIRECTORY_PREFIX
                        )
                    ):
                        observed = os.stat(
                            source,
                            dir_fd=src_dir_fd,
                            follow_symlinks=False,
                        )
                        original_identity = (
                            observed.st_dev,
                            observed.st_ino,
                        )
                        restored_name = source_name
                        displaced_name = (
                            "displaced-"
                            + source_name.removeprefix(".")
                        )
                        os.rename(
                            source,
                            displaced_name,
                            src_dir_fd=src_dir_fd,
                            dst_dir_fd=src_dir_fd,
                        )
                        os.mkdir(source, 0o700, dir_fd=src_dir_fd)
                        replacement = os.stat(
                            source,
                            dir_fd=src_dir_fd,
                            follow_symlinks=False,
                        )
                        replacement_identity = (
                            replacement.st_dev,
                            replacement.st_ino,
                        )
                    real_replace(
                        source,
                        target,
                        src_dir_fd=src_dir_fd,
                        dst_dir_fd=dst_dir_fd,
                    )

                with patch.object(
                    publish_local,
                    "_atomic_rename_no_replace",
                    side_effect=substitute_public_empty_directory,
                ), self.assertRaisesRegex(
                    publish_local.PublishError,
                    "committed and verified.*unrecognized state",
                ):
                    publish_local.publish_skills(
                        source_root=generated,
                        dest_root=destination,
                        receipt_path=receipt,
                    )

                self.assertIsNotNone(displaced_name)
                self.assertIsNotNone(restored_name)
                self.assertIsNotNone(original_identity)
                self.assertIsNotNone(replacement_identity)
                assert displaced_name is not None
                assert restored_name is not None
                assert original_identity is not None
                assert replacement_identity is not None
                displaced = list(destination.rglob(displaced_name))
                restored = list(destination.rglob(restored_name))
                self.assertEqual(1, len(displaced))
                self.assertGreaterEqual(len(restored), 1)
                self.assertEqual(
                    original_identity,
                    (
                        displaced[0].stat().st_dev,
                        displaced[0].stat().st_ino,
                    ),
                )
                self.assertIn(
                    replacement_identity,
                    {
                        (path.stat().st_dev, path.stat().st_ino)
                        for path in restored
                    },
                )

    def test_final_transaction_public_name_reappearance_is_reported(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            real_replace = publish_local._atomic_rename_no_replace
            transaction_name: str | None = None
            foreign_marker: Path | None = None

            def reappear_after_transaction_private_move(
                source: str | bytes,
                target: str | bytes,
                *,
                src_dir_fd: int,
                dst_dir_fd: int,
            ) -> None:
                nonlocal foreign_marker, transaction_name
                real_replace(
                    source,
                    target,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
                source_name = os.fsdecode(source)
                target_name = os.fsdecode(target)
                if (
                    transaction_name is None
                    and source_name.startswith(
                        publish_local.TRANSACTION_PREFIX
                    )
                    and target_name.startswith(
                        publish_local.CLEANUP_DIRECTORY_PREFIX
                    )
                ):
                    os.mkdir(source_name, 0o700, dir_fd=src_dir_fd)
                    transaction_name = source_name
                    foreign_marker = (
                        destination
                        / source_name
                        / "foreign-after-move.txt"
                    )
                    foreign_marker.write_text(
                        "preserve post-move publication bytes\n",
                        encoding="utf-8",
                    )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=reappear_after_transaction_private_move,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "committed and verified.*unrecognized state",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertIsNotNone(transaction_name)
            self.assertIsNotNone(foreign_marker)
            assert transaction_name is not None
            assert foreign_marker is not None
            self.assertEqual(
                "preserve post-move publication bytes\n",
                foreign_marker.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                [transaction_name],
                [
                    path.name
                    for path in destination.glob(
                        f"{publish_local.TRANSACTION_PREFIX}*"
                    )
                ],
            )
            for folder in release_state.SKILL_FOLDERS:
                self.assertIn(
                    "new",
                    (
                        destination / folder / "SKILL.md"
                    ).read_text(encoding="utf-8"),
                )

    def test_root_substitution_after_verified_swaps_is_not_reported_as_success(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            moved_destination = root / "moved-skills"
            unintended_target = root / "unintended"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            write_skill_tree(unintended_target, "attacker")
            unintended_before = snapshot(unintended_target)
            self.create_receipt(generated, receipt)
            real_swap_all = publish_local._swap_all

            def swap_then_substitute_root(
                *args: object,
                **kwargs: object,
            ) -> None:
                real_swap_all(*args, **kwargs)
                destination.rename(moved_destination)
                destination.symlink_to(
                    unintended_target,
                    target_is_directory=True,
                )

            stderr = io.StringIO()
            with patch.object(
                publish_local,
                "_swap_all",
                side_effect=swap_then_substitute_root,
            ), redirect_stderr(stderr), self.assertRaises(
                publish_local.PublishError
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(destination.is_symlink())
            self.assertEqual(unintended_target.resolve(), destination.resolve())
            self.assertEqual(unintended_before, snapshot(unintended_target))
            self.assertIn(
                "new",
                (
                    moved_destination / "stata-core" / "SKILL.md"
                ).read_text(encoding="utf-8"),
            )

    def test_post_commit_directory_close_error_is_a_warning_not_failure(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            expected_receipt = self.create_receipt(generated, receipt)
            real_close_directory = publish_local._close_directory_handle
            injected = False

            def report_post_commit_close_error(
                handle: publish_local.DirectoryHandle | None,
            ) -> list[str]:
                nonlocal injected
                close_errors = real_close_directory(handle)
                if (
                    handle is not None
                    and handle.name == "quarantine"
                    and not injected
                ):
                    injected = True
                    close_errors.append(
                        "forced post-commit directory close report"
                    )
                return close_errors

            stderr = io.StringIO()
            observed_receipt: dict | None = None
            observed_error: publish_local.PublishError | None = None
            with patch.object(
                publish_local,
                "_close_directory_handle",
                side_effect=report_post_commit_close_error,
            ), redirect_stderr(stderr):
                try:
                    observed_receipt = publish_local.publish_skills(
                        source_root=generated,
                        dest_root=destination,
                        receipt_path=receipt,
                    )
                except publish_local.PublishError as error:
                    observed_error = error

            self.assertTrue(injected)
            self.assertIsNone(observed_error)
            self.assertEqual(expected_receipt, observed_receipt)
            self.assertIn(
                "forced post-commit directory close report",
                stderr.getvalue(),
            )
            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(
                    snapshot(generated / folder),
                    snapshot(destination / folder),
                )

    def test_lock_release_warning_does_not_promise_an_unperformed_retry(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            expected_receipt = self.create_receipt(generated, receipt)
            real_close_descriptor = publish_local._close_file_descriptor
            failed_descriptor: int | None = None

            def report_first_lock_close_error(
                file_descriptor: int,
                *,
                unlock: bool = False,
            ) -> list[str]:
                nonlocal failed_descriptor
                if unlock and failed_descriptor is None:
                    failed_descriptor = file_descriptor
                    return ["forced lock descriptor close failure"]
                return real_close_descriptor(
                    file_descriptor,
                    unlock=unlock,
                )

            stderr = io.StringIO()
            with patch.object(
                publish_local,
                "_close_file_descriptor",
                side_effect=report_first_lock_close_error,
            ), redirect_stderr(stderr):
                observed_receipt = publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(expected_receipt, observed_receipt)
            self.assertIsNotNone(failed_descriptor)
            assert failed_descriptor is not None
            try:
                os.fstat(failed_descriptor)
            except OSError as error:
                self.assertEqual(errno.EBADF, error.errno)
                descriptor_remained_open = False
            else:
                descriptor_remained_open = True
                real_close_descriptor(failed_descriptor, unlock=True)

            warning = stderr.getvalue()
            self.assertIn("forced lock descriptor close failure", warning)
            self.assertFalse(
                descriptor_remained_open
                and "finalizer will attempt" in warning,
                "warning promised finalizer closure but the descriptor "
                "remained open",
            )

    def test_sentinel_close_error_is_reported(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            real_open = os.open
            real_close = os.close
            sentinel_descriptor: int | None = None
            close_failure_injected = False

            def record_sentinel_open(
                path: str | bytes | Path,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal sentinel_descriptor
                file_descriptor = real_open(
                    path,
                    flags,
                    mode,
                    dir_fd=dir_fd,
                )
                if path == publish_local.TRANSACTION_LOCK_NAME:
                    sentinel_descriptor = file_descriptor
                return file_descriptor

            def fail_sentinel_close(file_descriptor: int) -> None:
                nonlocal close_failure_injected
                if (
                    file_descriptor == sentinel_descriptor
                    and not close_failure_injected
                ):
                    close_failure_injected = True
                    raise OSError("forced sentinel descriptor close failure")
                real_close(file_descriptor)

            stderr = io.StringIO()
            observed_error: publish_local.PublishError | None = None
            try:
                with patch.object(
                    publish_local.os,
                    "open",
                    side_effect=record_sentinel_open,
                ), patch.object(
                    publish_local.os,
                    "close",
                    side_effect=fail_sentinel_close,
                ), redirect_stderr(stderr):
                    try:
                        publish_local.publish_skills(
                            source_root=generated,
                            dest_root=destination,
                            receipt_path=receipt,
                        )
                    except publish_local.PublishError as error:
                        observed_error = error
            finally:
                if sentinel_descriptor is not None:
                    try:
                        os.fstat(sentinel_descriptor)
                    except OSError:
                        pass
                    else:
                        real_close(sentinel_descriptor)

            self.assertTrue(close_failure_injected)
            surfaced_diagnostic = (
                observed_error is not None
                and "forced sentinel descriptor close failure"
                in str(observed_error)
            ) or (
                "forced sentinel descriptor close failure"
                in stderr.getvalue()
            )
            self.assertTrue(
                surfaced_diagnostic,
                "sentinel descriptor close failure was silently discarded",
            )

    def test_substituted_transaction_directory_is_not_recursively_deleted(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            external = root / "external"
            displaced_created_directory = root / "created-transaction-directory"
            valuable = external / "valuable.txt"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            external.mkdir()
            valuable.write_text("preserve external bytes\n", encoding="utf-8")
            self.create_receipt(generated, receipt)
            real_open_directory = publish_local._open_directory_handle_at
            substituted = False

            def substitute_before_transaction_open(
                parent_descriptor: int,
                name: str,
                display_path: Path,
            ) -> publish_local.DirectoryHandle:
                nonlocal substituted
                if (
                    not substituted
                    and name.startswith(publish_local.TRANSACTION_PREFIX)
                ):
                    substituted = True
                    display_path.rename(displaced_created_directory)
                    external.rename(display_path)
                return real_open_directory(
                    parent_descriptor,
                    name,
                    display_path,
                )

            with patch.object(
                publish_local,
                "_open_directory_handle_at",
                side_effect=substitute_before_transaction_open,
            ), self.assertRaises(publish_local.PublishError):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertTrue(substituted)
            surviving_values = [
                path.read_text(encoding="utf-8")
                for path in root.rglob("valuable.txt")
            ]
            self.assertEqual(["preserve external bytes\n"], surviving_values)

    def test_interrupt_after_committed_backup_rename_preserves_original(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            expected_core = snapshot(destination / "stata-core")
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0

            def interrupt_after_first_backup(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                real_replace(source, target, **kwargs)
                if replace_calls == 1:
                    raise KeyboardInterrupt(
                        "interrupt after committed backup rename"
                    )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=interrupt_after_first_backup,
            ), self.assertRaises(publish_local.PublishError):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            observed_copies = []
            installed_core = destination / "stata-core"
            if installed_core.is_dir():
                observed_copies.append(snapshot(installed_core))
            for transaction in destination.glob(
                f"{publish_local.TRANSACTION_PREFIX}*"
            ):
                backup_core = transaction / "backups" / "stata-core"
                if backup_core.is_dir():
                    observed_copies.append(snapshot(backup_core))
            self.assertIn(expected_core, observed_copies)

    def test_post_backup_substitution_preserves_displaced_original(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            expected_core = snapshot(destination / "stata-core")
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0
            displaced_original: Path | None = None
            substituted_backup: Path | None = None

            def substitute_after_first_backup(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal displaced_original, replace_calls, substituted_backup
                real_replace(source, target, **kwargs)
                replace_calls += 1
                if replace_calls == 1:
                    transaction = next(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                    substituted_backup = (
                        transaction / "backups" / "stata-core"
                    )
                    displaced_original = (
                        transaction / "displaced-original-stata-core"
                    )
                    substituted_backup.rename(displaced_original)
                    substituted_backup.mkdir()
                    (substituted_backup / "owner.txt").write_text(
                        "preserve substituted backup bytes\n",
                        encoding="utf-8",
                    )

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=substitute_after_first_backup,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "rollback was incomplete",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertIsNotNone(displaced_original)
            self.assertIsNotNone(substituted_backup)
            assert displaced_original is not None
            assert substituted_backup is not None
            self.assertEqual(expected_core, snapshot(displaced_original))
            self.assertEqual(
                "preserve substituted backup bytes\n",
                (substituted_backup / "owner.txt").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertFalse((destination / "stata-core").exists())
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_changed_backup_is_preserved_instead_of_restored(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            real_replace = publish_local._atomic_rename_no_replace
            replace_calls = 0
            changed_backup = "# Concurrent backup bytes\n"

            def change_backup_then_fail_install(
                source: str | Path,
                target: str | Path,
                **kwargs: int,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 2:
                    transaction = next(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                    (
                        transaction
                        / "backups"
                        / "stata-core"
                        / "SKILL.md"
                    ).write_text(changed_backup, encoding="utf-8")
                    raise OSError("forced install failure after backup drift")
                real_replace(source, target, **kwargs)

            with patch.object(
                publish_local,
                "_atomic_rename_no_replace",
                side_effect=change_backup_then_fail_install,
            ), self.assertRaises(publish_local.PublishError) as raised:
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertIn("rollback was incomplete", str(raised.exception))
            transactions = list(
                destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")
            )
            self.assertEqual(1, len(transactions))
            self.assertEqual(
                changed_backup,
                (
                    transactions[0]
                    / "backups"
                    / "stata-core"
                    / "SKILL.md"
                ).read_text(encoding="utf-8"),
            )

    def test_recursive_cleanup_closes_child_descriptor_after_failure(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            transaction_path = root / "transaction"
            leaf = transaction_path / "nested" / "leaf.txt"
            leaf.parent.mkdir(parents=True)
            leaf.write_text("preserve recovery bytes\n", encoding="utf-8")
            root_descriptor = os.open(
                root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            transaction = publish_local._open_directory_handle_at(
                root_descriptor,
                transaction_path.name,
                transaction_path,
            )
            real_open_directory = publish_local._open_directory_handle_at
            real_unlink = os.unlink
            descendant_descriptors: list[int] = []

            def record_descendant_descriptor(
                parent_descriptor: int,
                name: str,
                display_path: Path,
            ) -> publish_local.DirectoryHandle:
                handle = real_open_directory(
                    parent_descriptor,
                    name,
                    display_path,
                )
                if handle.file_descriptor is not None:
                    descendant_descriptors.append(handle.file_descriptor)
                return handle

            def fail_leaf_unlink(
                name: str | bytes,
                *,
                dir_fd: int | None = None,
            ) -> None:
                if name == "leaf.txt":
                    raise PermissionError("forced nested cleanup failure")
                real_unlink(name, dir_fd=dir_fd)

            open_after_failure: list[int] = []
            try:
                with patch.object(
                    publish_local,
                    "_open_directory_handle_at",
                    side_effect=record_descendant_descriptor,
                ), patch.object(
                    publish_local.os,
                    "unlink",
                    side_effect=fail_leaf_unlink,
                ), self.assertRaises((OSError, publish_local.PublishError)):
                    publish_local._clear_directory_handle(transaction)

                self.assertTrue(leaf.is_file())
                for file_descriptor in descendant_descriptors:
                    try:
                        os.fstat(file_descriptor)
                    except OSError as error:
                        self.assertEqual(errno.EBADF, error.errno)
                    else:
                        open_after_failure.append(file_descriptor)
            finally:
                for file_descriptor in open_after_failure:
                    os.close(file_descriptor)
                publish_local._close_directory_handle(transaction)
                os.close(root_descriptor)

            self.assertEqual([], open_after_failure)

    def test_cleanup_failure_warns_after_verified_publish(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            expected_receipt = self.create_receipt(generated, receipt)

            stderr = io.StringIO()
            with patch.object(
                publish_local,
                "_remove_transaction_workspace",
                side_effect=PermissionError("forced transaction cleanup failure"),
            ), redirect_stderr(stderr):
                observed_receipt = publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(expected_receipt, observed_receipt)
            self.assertIn("publication committed and verified", stderr.getvalue())
            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(
                    snapshot(generated / folder),
                    snapshot(destination / folder),
                )
            self.assertEqual(
                1,
                len(
                    list(
                        destination.glob(
                            f"{publish_local.TRANSACTION_PREFIX}*"
                        )
                    )
                ),
            )

    def test_lock_cleanup_failure_warns_after_verified_publish(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            expected_receipt = self.create_receipt(generated, receipt)
            stderr = io.StringIO()

            with patch.object(
                publish_local,
                "_release_transaction_lock",
                side_effect=PermissionError("forced lock cleanup failure"),
            ), redirect_stderr(stderr):
                observed_receipt = publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(expected_receipt, observed_receipt)
            self.assertIn(
                "kernel transaction-lock release state is indeterminate",
                stderr.getvalue(),
            )
            for folder in release_state.SKILL_FOLDERS:
                self.assertEqual(
                    snapshot(generated / folder),
                    snapshot(destination / folder),
                )
            self.assertTrue(
                (destination / publish_local.TRANSACTION_LOCK_NAME).is_file()
            )
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )


if __name__ == "__main__":
    unittest.main()
