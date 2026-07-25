from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
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


class ValidationReceiptCliTests(unittest.TestCase):
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
        write_receipt.assert_called_once_with(
            build_root=build_root,
            receipt_path=receipt,
            expected_state={
                "source_sha256": "source",
                "tree_sha256": "tree",
            },
        )

    def test_failed_gate_removes_prior_receipt(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            receipt = build_root.parent / "validation-receipt.json"
            receipt.parent.mkdir(parents=True)
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

        self.assertEqual(1, result)
        self.assertFalse(receipt.exists())
        write_receipt.assert_not_called()

    def test_filtered_validation_cannot_write_release_receipt(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
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

    def test_cleanup_failure_prevents_receipt_and_reports_workdir(self) -> None:
        with TemporaryDirectory(prefix="validation-receipt-") as temp_root:
            build_root = Path(temp_root) / "build" / "generated"
            receipt = build_root.parent / "validation-receipt.json"
            work_root = Path(temp_root) / "validation-work"
            work_root.mkdir()
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
                validate_skill_pack.tempfile,
                "mkdtemp",
                return_value=str(work_root),
            ), patch.object(
                validate_skill_pack.shutil,
                "rmtree",
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
        self.assertIn("validation workspace cleanup: FAIL", output.getvalue())
        self.assertIn(
            "validation workdir cleanup failed; inspect the preserved path",
            output.getvalue(),
        )
        self.assertIn(str(work_root), output.getvalue())

    def test_make_validate_invalidates_receipt_before_failed_check(self) -> None:
        with TemporaryDirectory(prefix="validation-make-") as temp_root:
            root = Path(temp_root)
            shutil.copyfile(REPO_ROOT / "Makefile", root / "Makefile")
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
                ["git", "add", ".gitignore", "Makefile"],
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

            result = subprocess.run(
                [
                    "make",
                    "validate",
                    "UV=false",
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertFalse(receipt.exists())
            self.assertIn("false lock --check --offline", result.stdout)

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
