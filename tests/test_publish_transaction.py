from __future__ import annotations

from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
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
                if candidate == destination / "stata-packages":
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


if __name__ == "__main__":
    unittest.main()
