from __future__ import annotations

import ast
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import doctor  # noqa: E402
import libskillpack  # noqa: E402
import runtime_guard  # noqa: E402


class DoctorTests(unittest.TestCase):
    def test_python_minor_is_pinned_for_unicode_reproducibility(self) -> None:
        self.assertIsNone(
            libskillpack.runtime_compatibility_error(
                (3, 11, 15),
                "14.0.0",
            )
        )
        for version, unicode_version in (
            ((3, 10, 19), "14.0.0"),
            ((3, 12, 0), "15.0.0"),
            ((3, 14, 3), "16.0.0"),
            ((3, 11, 15), "16.0.0"),
        ):
            with self.subTest(
                version=version,
                unicode_version=unicode_version,
            ):
                self.assertIsNotNone(
                    libskillpack.runtime_compatibility_error(
                        version,
                        unicode_version,
                    )
                )

    def test_every_cli_guards_runtime_before_sensitive_imports(self) -> None:
        scripts = sorted((REPO_ROOT / "scripts").glob("*.py"))
        cli_scripts = [
            path
            for path in scripts
            if 'if __name__ == "__main__":' in path.read_text(encoding="utf-8")
        ]
        self.assertTrue(cli_scripts)
        for path in cli_scripts:
            with self.subTest(script=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                guard_call = next(
                    node
                    for node in tree.body
                    if isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "require_supported_runtime"
                )
                sensitive_imports = [
                    node
                    for node in tree.body
                    if isinstance(node, ast.ImportFrom)
                    and node.module
                    in {
                        "jinja2",
                        "libskillpack",
                        "release_state",
                        "render_skills",
                        "validate_skill_pack",
                    }
                    or isinstance(node, ast.Import)
                    and any(
                        alias.name in {"jinja2", "yaml"}
                        for alias in node.names
                    )
                ]
                self.assertTrue(sensitive_imports)
                self.assertLess(
                    guard_call.lineno,
                    min(node.lineno for node in sensitive_imports),
                )

    def test_runtime_guard_fails_closed_with_clean_diagnostic(self) -> None:
        with patch.object(
            runtime_guard,
            "runtime_compatibility_error",
            return_value="unsupported test runtime",
        ), self.assertRaisesRegex(
            SystemExit,
            "ERROR: unsupported test runtime",
        ):
            libskillpack.require_supported_runtime()

    def test_required_uv_version_must_be_one_exact_version(self) -> None:
        with TemporaryDirectory(prefix="doctor-version-") as temp_root:
            root = Path(temp_root)
            pyproject = root / "pyproject.toml"
            pyproject.write_text(
                '[tool.uv]\nrequired-version = "==0.11.11"\n',
                encoding="utf-8",
            )
            with patch.object(doctor, "REPO_ROOT", root):
                self.assertEqual("0.11.11", doctor.required_uv_version())

            pyproject.write_text(
                '[tool.uv]\nrequired-version = ">=0.11"\n',
                encoding="utf-8",
            )
            with patch.object(doctor, "REPO_ROOT", root), self.assertRaisesRegex(
                ValueError,
                "one exact",
            ):
                doctor.required_uv_version()

    def test_installed_uv_version_rejects_unexpected_output(self) -> None:
        with patch.object(
            doctor.subprocess,
            "run",
            return_value=CompletedProcess(
                ["uv", "--version"],
                0,
                "uv 0.11.11 (Homebrew build)\n",
                "",
            ),
        ):
            self.assertEqual("0.11.11", doctor.installed_uv_version("uv"))

        with patch.object(
            doctor.subprocess,
            "run",
            return_value=CompletedProcess(
                ["uv", "--version"],
                0,
                "unknown\n",
                "",
            ),
        ), self.assertRaisesRegex(ValueError, "unexpected uv version output"):
            doctor.installed_uv_version("uv")

        with patch.object(
            doctor.subprocess,
            "run",
            side_effect=doctor.subprocess.TimeoutExpired(["uv"], 10),
        ), self.assertRaisesRegex(ValueError, "could not run uv --version"):
            doctor.installed_uv_version("uv")


if __name__ == "__main__":
    unittest.main()
