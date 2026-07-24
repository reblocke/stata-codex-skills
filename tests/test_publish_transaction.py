from __future__ import annotations

from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shutil
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
        if path.is_file()
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

            with patch.object(
                publish_local.os,
                "replace",
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

            self.assertEqual(before, snapshot(destination))
            self.assertEqual("concurrent lock\n", lock_path.read_text(encoding="utf-8"))
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
                publish_local.os,
                "replace",
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

    def test_copy_failure_is_reported_cleanly_and_cleans_transaction(self) -> None:
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
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

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
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_mid_swap_failure_rolls_back_every_skill_and_cleans_transaction(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            before = snapshot(destination)
            real_replace = os.replace

            def fail_second_install(source: str | Path, target: str | Path) -> None:
                source_path = Path(source)
                if (
                    source_path.parent.name == "stage"
                    and source_path.name == "stata-packages"
                ):
                    raise OSError("forced second install failure")
                real_replace(source, target)

            with patch.object(
                publish_local.os,
                "replace",
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

            self.assertEqual(before, snapshot(destination))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
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

            with patch.object(
                publish_local,
                "_stage_all",
                side_effect=stage_then_mutate,
            ), patch.object(
                publish_local.os,
                "replace",
                side_effect=AssertionError("destination swap attempted"),
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
            self.assertFalse(
                (destination / publish_local.TRANSACTION_LOCK_NAME).exists()
            )
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
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

            with patch.object(
                publish_local,
                "_stage_all",
                side_effect=stage_then_replace_with_symlink,
            ), patch.object(
                publish_local.os,
                "replace",
                side_effect=AssertionError("destination swap attempted"),
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
            self.assertFalse(
                (destination / publish_local.TRANSACTION_LOCK_NAME).exists()
            )
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_concurrent_lock_replacement_aborts_before_any_swap(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            self.create_receipt(generated, receipt)
            before = snapshot(destination)
            real_preflight = publish_local.preflight_destinations
            preflight_calls = 0

            def replace_lock_after_initial_assertion(
                destination_root: Path,
            ) -> dict[str, publish_local.DestinationState]:
                nonlocal preflight_calls
                states = real_preflight(destination_root)
                preflight_calls += 1
                if preflight_calls == 2:
                    lock_path = (
                        destination / publish_local.TRANSACTION_LOCK_NAME
                    )
                    lock_path.unlink()
                    lock_path.write_text(
                        "concurrent lock\n",
                        encoding="utf-8",
                    )
                return states

            with patch.object(
                publish_local,
                "preflight_destinations",
                side_effect=replace_lock_after_initial_assertion,
            ), patch.object(
                publish_local.os,
                "replace",
                side_effect=AssertionError("destination swap attempted"),
            ), self.assertRaises(
                publish_local.PublishError,
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            lock_path = destination / publish_local.TRANSACTION_LOCK_NAME
            self.assertEqual("concurrent lock\n", lock_path.read_text(encoding="utf-8"))
            skill_prefixes = tuple(
                f"{folder}/" for folder in release_state.SKILL_FOLDERS
            )
            self.assertEqual(
                {
                    path: payload
                    for path, payload in before.items()
                    if path.startswith(skill_prefixes)
                },
                {
                    path: payload
                    for path, payload in snapshot(destination).items()
                    if path.startswith(skill_prefixes)
                },
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
            unintended_target.mkdir()
            marker = unintended_target / "owner.txt"
            marker.write_text("concurrent owner\n", encoding="utf-8")
            self.create_receipt(generated, receipt)
            real_replace = os.replace
            replace_calls = 0

            def substitute_root_on_third_replace(
                source: str | Path,
                target: str | Path,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 3:
                    destination.rename(moved_destination)
                    destination.symlink_to(
                        unintended_target,
                        target_is_directory=True,
                    )
                    raise OSError("forced failure after root substitution")
                real_replace(source, target)

            with patch.object(
                publish_local.os,
                "replace",
                side_effect=substitute_root_on_third_replace,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "rollback was incomplete",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(3, replace_calls)
            self.assertTrue(destination.is_symlink())
            self.assertEqual(unintended_target.resolve(), destination.resolve())
            self.assertEqual("concurrent owner\n", marker.read_text(encoding="utf-8"))
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

    def test_later_child_change_after_first_swap_survives_and_preserves_recovery(
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
            real_replace = os.replace
            replace_calls = 0

            def mutate_later_child_after_first_install(
                source: str | Path,
                target: str | Path,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                real_replace(source, target)
                if replace_calls == 2:
                    concurrent_file.write_text(
                        "# Concurrent package bytes\n",
                        encoding="utf-8",
                    )

            with patch.object(
                publish_local.os,
                "replace",
                side_effect=mutate_later_child_after_first_install,
            ), self.assertRaisesRegex(
                publish_local.PublishError,
                "rollback was incomplete",
            ):
                publish_local.publish_skills(
                    source_root=generated,
                    dest_root=destination,
                    receipt_path=receipt,
                )

            self.assertEqual(3, replace_calls)
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
            self.assertTrue(
                (destination / publish_local.TRANSACTION_LOCK_NAME).is_file()
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
            real_replace = os.replace
            replace_calls = 0

            def mutate_first_installed_child_after_all_installs(
                source: str | Path,
                target: str | Path,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                real_replace(source, target)
                if replace_calls == 6:
                    concurrent_file.write_text(
                        "# Concurrent installed bytes\n",
                        encoding="utf-8",
                    )

            with patch.object(
                publish_local.os,
                "replace",
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

            self.assertEqual(8, replace_calls)
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
            before = snapshot(destination)
            real_tree_digest = publish_local.tree_digest

            def corrupt_installed_digest(path: Path) -> str:
                candidate = Path(path)
                if candidate == destination.resolve() / "stata-packages":
                    return "0" * 64
                return real_tree_digest(candidate)

            with patch.object(
                publish_local,
                "tree_digest",
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

            self.assertEqual(before, snapshot(destination))
            self.assertEqual(
                [],
                list(destination.glob(f"{publish_local.TRANSACTION_PREFIX}*")),
            )

    def test_cleanup_failure_warns_after_verified_publish(self) -> None:
        with TemporaryDirectory(prefix="publish-test-") as temporary:
            root = Path(temporary)
            generated = root / "generated"
            destination = root / "skills"
            receipt = root / "receipt.json"
            write_skill_tree(generated, "new")
            write_skill_tree(destination, "old")
            expected_receipt = self.create_receipt(generated, receipt)
            real_rmtree = publish_local.shutil.rmtree

            def fail_transaction_cleanup(path: str | Path, *args: object, **kwargs: object) -> None:
                candidate = Path(path)
                if candidate.name.startswith(publish_local.TRANSACTION_PREFIX):
                    raise PermissionError("forced transaction cleanup failure")
                real_rmtree(candidate, *args, **kwargs)

            stderr = io.StringIO()
            with patch.object(
                publish_local.shutil,
                "rmtree",
                side_effect=fail_transaction_cleanup,
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
                "transaction lock could not be removed",
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
