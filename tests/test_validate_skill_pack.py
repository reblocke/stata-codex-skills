from __future__ import annotations

from contextlib import chdir, contextmanager
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory
import os
import sys
import unittest
from unittest.mock import Mock, patch


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import validate_skill_pack  # noqa: E402


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


class ValidateCoreTests(unittest.TestCase):
    def test_validate_core_builds_one_do_file_per_content_entry(self) -> None:
        with TemporaryDirectory(prefix="validate-work-") as work_root:
            work_root_path = Path(work_root)
            entries = [
                {"slug": "first", "order": 1, "smoke_test": "display 1"},
                {"slug": "second", "order": 2, "smoke_test": "display 2"},
            ]
            calls: list[tuple[Path, Path, str, str]] = []

            def fake_run_stata_do(
                stata_binary: Path,
                do_file: Path,
                cwd: Path,
                completion_marker: str,
                timeout_seconds: int = 90,
            ) -> tuple[CompletedProcess[str], Path]:
                del stata_binary, timeout_seconds
                do_text = do_file.read_text(encoding="utf-8")
                calls.append((do_file, cwd, completion_marker, do_text))
                log_path = cwd / f"{do_file.stem}.log"
                slug = cwd.name
                log_path.write_text(
                    f"PASS: {slug}\n{completion_marker}\n",
                    encoding="utf-8",
                )
                return CompletedProcess(["stata"], 0, "", ""), log_path

            with patch.object(validate_skill_pack, "core_content_entries", return_value=entries), patch.object(
                validate_skill_pack, "run_stata_do", side_effect=fake_run_stata_do
            ):
                results = validate_skill_pack.validate_core(
                    Path("/fake/stata"), work_root_path
                )

            self.assertEqual(
                [("first", True), ("second", True)],
                [(slug, success) for slug, success, _ in results],
            )
            self.assertEqual(2, len(calls))
            self.assertNotEqual(calls[0][2], calls[1][2])
            self.assertIn(calls[0][2], calls[0][3])
            self.assertIn(calls[1][2], calls[1][3])
            self.assertIn("display 1", calls[0][3])
            self.assertIn("display 2", calls[1][3])
            self.assertEqual(work_root_path / "core" / "first", calls[0][1])
            self.assertEqual(work_root_path / "core" / "second", calls[1][1])

    def test_validate_core_propagates_runner_marker_failure(self) -> None:
        with TemporaryDirectory(prefix="validate-work-") as work_root:
            def fake_run_stata_do(
                stata_binary: Path,
                do_file: Path,
                cwd: Path,
                completion_marker: str,
                timeout_seconds: int = 90,
            ) -> tuple[CompletedProcess[str], Path]:
                del stata_binary, completion_marker, timeout_seconds
                log_path = cwd / f"{do_file.stem}.log"
                log_path.write_text("VALIDATION COMPLETE\n", encoding="utf-8")
                return CompletedProcess(
                    ["stata"],
                    1,
                    "",
                    "Stata log did not contain the exact completion marker.",
                ), log_path

            with patch.object(
                validate_skill_pack,
                "core_content_entries",
                return_value=[
                    {"slug": "sample", "order": 1, "smoke_test": "display 1"}
                ],
            ), patch.object(
                validate_skill_pack, "run_stata_do", side_effect=fake_run_stata_do
            ):
                results = validate_skill_pack.validate_core(
                    Path("/fake/stata"), Path(work_root)
                )

            self.assertEqual(1, len(results))
            self.assertFalse(results[0][1])

    def test_validate_packages_uses_child_relative_plus_path(self) -> None:
        with TemporaryDirectory(prefix="validate-package-path-") as temp_root:
            root = Path(temp_root)
            entry = {
                "slug": "sample",
                "install_commands": [],
                "preflight_commands": [],
                "smoke_test": "display 1",
            }
            observed_do_text = ""

            def fake_run_stata_do(
                stata_binary: Path,
                do_file: Path,
                cwd: Path,
                completion_marker: str,
                timeout_seconds: int = 180,
            ) -> tuple[CompletedProcess[str], Path]:
                nonlocal observed_do_text
                del stata_binary, timeout_seconds
                observed_do_text = do_file.read_text(encoding="utf-8")
                log_path = cwd / f"{do_file.stem}.log"
                log_path.write_text(
                    f"PASS: sample\n{completion_marker}\n",
                    encoding="utf-8",
                )
                return CompletedProcess(["stata"], 0, "", ""), log_path

            with chdir(root), patch.object(
                validate_skill_pack,
                "package_content_entries",
                return_value=[entry],
            ), patch.object(
                validate_skill_pack,
                "run_stata_do",
                side_effect=fake_run_stata_do,
            ), patch.object(
                validate_skill_pack,
                "diagnostics_alias_check",
                return_value=(True, ""),
            ):
                results = validate_skill_pack.validate_packages(
                    Path("/fake/stata"),
                    Path("."),
                )

            self.assertTrue(results[0][1])
            self.assertIn('sysdir set PLUS "plus"', observed_do_text)
            self.assertNotIn(
                'sysdir set PLUS "packages/sample/plus"',
                observed_do_text,
            )

    def test_plugin_runtime_uses_path_relative_to_child_workdir(self) -> None:
        with TemporaryDirectory(prefix="validate-plugin-path-") as temp_root:
            root = Path(temp_root)
            plugin_path = Path("plugins/compile/hello.plugin")
            observed_do_text = ""

            def fake_run_stata_do(
                stata_binary: Path,
                do_file: Path,
                cwd: Path,
                completion_marker: str,
                timeout_seconds: int = 30,
            ) -> tuple[CompletedProcess[str], Path]:
                nonlocal observed_do_text
                del stata_binary, timeout_seconds
                observed_do_text = do_file.read_text(encoding="utf-8")
                log_path = cwd / f"{do_file.stem}.log"
                log_path.write_text(
                    f"CODEX_PLUGIN_PHASE::{completion_marker.rsplit('::', 1)[-1]}::before-load\n"
                    f"CODEX_PLUGIN_PHASE::{completion_marker.rsplit('::', 1)[-1]}::after-load\n"
                    f"CODEX_PLUGIN_PHASE::{completion_marker.rsplit('::', 1)[-1]}::before-call\n"
                    "Hello World\n"
                    f"CODEX_PLUGIN_PHASE::{completion_marker.rsplit('::', 1)[-1]}::after-call\n"
                    f"PASS: plugin-smoke\n{completion_marker}\n",
                    encoding="utf-8",
                )
                return CompletedProcess(["stata"], 0, "", ""), log_path

            with chdir(root):
                plugin_path.parent.mkdir(parents=True)
                plugin_path.write_bytes(b"plugin")
                with patch.object(
                    validate_skill_pack,
                    "run_stata_do",
                    side_effect=fake_run_stata_do,
                ):
                    success, _ = (
                        validate_skill_pack.validate_plugin_runtime(
                            Path("/fake/stata"),
                            Path("."),
                            plugin_path,
                        )
                    )

            self.assertTrue(success)
            self.assertIn(
                'plugin using("../compile/hello.plugin")',
                observed_do_text,
            )
            self.assertIn("plugin call hello", observed_do_text.splitlines())
            self.assertNotIn("hello", observed_do_text.splitlines())


class ValidatePluginRuntimeTests(unittest.TestCase):
    MARKER = "CODEX_VALIDATION_COMPLETE::PLUGIN_RUNTIME::test-run"
    PHASE = "CODEX_PLUGIN_PHASE::test-run"

    def valid_log_lines(self) -> list[str]:
        return [
            f"{self.PHASE}::before-load",
            f"{self.PHASE}::after-load",
            f"{self.PHASE}::before-call",
            "Hello World",
            f"{self.PHASE}::after-call",
            "PASS: plugin-smoke",
            self.MARKER,
        ]

    def validate_log(self, lines: list[str], returncode: int = 0) -> bool:
        with TemporaryDirectory(prefix="validate-plugin-evidence-") as temporary:
            root = Path(temporary)

            def fake_run(stata_binary, do_file, cwd, completion_marker,
                         timeout_seconds):
                self.assertEqual(timeout_seconds, 30)
                self.assertEqual(completion_marker, self.MARKER)
                log_path = cwd / f"{do_file.stem}.log"
                log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return CompletedProcess(["stata"], returncode, "", ""), log_path

            with patch.object(
                validate_skill_pack, "completion_marker", return_value=self.MARKER
            ), patch.object(
                validate_skill_pack, "run_stata_do", side_effect=fake_run
            ):
                success, _ = validate_skill_pack.validate_plugin_runtime(
                    Path("/fake/stata"), root, root / "hello.plugin"
                )
            return success

    def test_plugin_do_file_marks_loading_and_calling_in_order(self) -> None:
        lines = validate_skill_pack.plugin_do_text(
            Path("../compile/hello.plugin"), self.MARKER
        ).splitlines()
        expected = [
            f'display "{self.PHASE}::before-load"',
            'program hello, plugin using("../compile/hello.plugin")',
            f'display "{self.PHASE}::after-load"',
            f'display "{self.PHASE}::before-call"',
            "plugin call hello",
            f'display "{self.PHASE}::after-call"',
        ]
        self.assertEqual(lines[2:8], expected)

    def test_plugin_runtime_accepts_complete_callback_evidence(self) -> None:
        self.assertTrue(self.validate_log(self.valid_log_lines()))

    def test_plugin_runtime_rejects_missing_or_reordered_evidence(self) -> None:
        valid = self.valid_log_lines()
        invalid = {
            "missing callback": [line for line in valid if line != "Hello World"],
            "echoed callback": [
                '. display "Hello World"' if line == "Hello World" else line
                for line in valid
            ],
            "missing phase": valid[:1] + valid[2:],
            "reordered phases": [valid[1], valid[0], *valid[2:]],
            "missing completion": valid[:-1],
            "Stata error": [*valid, "r(199);"],
            "wrong run": [line.replace("test-run", "stale-run") for line in valid],
        }
        for label, lines in invalid.items():
            with self.subTest(label=label):
                self.assertFalse(self.validate_log(lines))

    def test_plugin_runtime_rejects_timeout_or_failed_cleanup(self) -> None:
        for returncode in (124, 1):
            with self.subTest(returncode=returncode):
                self.assertFalse(self.validate_log(self.valid_log_lines(), returncode))


class CliValidationTests(unittest.TestCase):
    def test_any_failed_suite_makes_main_nonzero_without_short_circuiting(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            work_root = Path(parent) / "run"
            work_root.mkdir()
            with patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_core",
                return_value=[("sample", False, "core failed")],
            ) as validate_core, patch.object(
                validate_skill_pack,
                "validate_packages",
                return_value=[("selected", True, "package passed")],
            ) as validate_packages, supplied_validation_workspace(work_root):
                exit_code = validate_skill_pack.main(
                    ["--suite", "core", "--suite", "packages", "--package", "selected"]
                )

            self.assertEqual(1, exit_code)
            validate_core.assert_called_once()
            validate_packages.assert_called_once()
            self.assertTrue(work_root.is_dir())

    def test_failed_package_result_makes_main_nonzero(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            work_root = Path(parent) / "run"
            work_root.mkdir()
            with patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_packages",
                return_value=[
                    ("failed-package", False, "install failed"),
                    ("passing-package", True, ""),
                ],
            ), supplied_validation_workspace(work_root):
                exit_code = validate_skill_pack.main(["--suite", "packages"])

            self.assertEqual(1, exit_code)

    def test_repeatable_package_option_is_forwarded_in_order(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            work_root = Path(parent) / "run"
            work_root.mkdir()
            package_validator = Mock(return_value=[("beta", True, ""), ("alpha", True, "")])
            with patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack, "validate_packages", package_validator
            ), supplied_validation_workspace(work_root):
                exit_code = validate_skill_pack.main(
                    [
                        "--suite",
                        "packages",
                        "--package",
                        "beta",
                        "--package",
                        "alpha",
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertEqual(["beta", "alpha"], package_validator.call_args.args[2])

    def test_default_runs_plugin_compile_but_not_plugin_runtime(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            work_root = Path(parent) / "run"
            work_root.mkdir()
            plugin_path = Path("plugins") / "hello.plugin"
            compile_validator = Mock(return_value=(True, "compiled", plugin_path))
            runtime_validator = Mock(return_value=(True, "executed"))
            with patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_core",
                return_value=[("sample", True, "")],
            ), patch.object(
                validate_skill_pack,
                "validate_packages",
                return_value=[("sample", True, "")],
            ), patch.object(
                validate_skill_pack, "validate_plugin_compile", compile_validator
            ), patch.object(
                validate_skill_pack, "validate_plugin_runtime", runtime_validator
            ), supplied_validation_workspace(work_root):
                exit_code = validate_skill_pack.main(["--suite", "default"])

            self.assertEqual(0, exit_code)
            compile_validator.assert_called_once()
            runtime_validator.assert_not_called()

    def test_empty_packages_suite_fails_instead_of_vacuously_passing(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            work_root = Path(parent) / "run"
            work_root.mkdir()
            with patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack, "validate_packages", return_value=[]
            ), supplied_validation_workspace(work_root):
                exit_code = validate_skill_pack.main(["--suite", "packages"])

            self.assertEqual(1, exit_code)

    def test_plugin_runtime_runs_only_when_explicitly_selected(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            work_root = Path(parent) / "run"
            work_root.mkdir()
            plugin_path = work_root / "plugins" / "hello.plugin"
            compile_validator = Mock(return_value=(True, "compiled", plugin_path))
            runtime_validator = Mock(return_value=(True, "executed"))
            with patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack, "validate_plugin_compile", compile_validator
            ), patch.object(
                validate_skill_pack, "validate_plugin_runtime", runtime_validator
            ), supplied_validation_workspace(work_root):
                exit_code = validate_skill_pack.main(["--suite", "plugin-runtime"])

            self.assertEqual(0, exit_code)
            compile_validator.assert_called_once()
            runtime_validator.assert_called_once_with(
                Path("/fake/stata"), Path("."), plugin_path
            )

    def test_failed_plugin_compile_is_nonzero_and_blocks_runtime(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            work_root = Path(parent) / "run"
            work_root.mkdir()
            runtime_validator = Mock(return_value=(True, "executed"))
            with patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_plugin_compile",
                return_value=(False, "compile failed", None),
            ), patch.object(
                validate_skill_pack, "validate_plugin_runtime", runtime_validator
            ), supplied_validation_workspace(work_root):
                exit_code = validate_skill_pack.main(
                    ["--suite", "plugin-runtime"]
                )

            self.assertEqual(1, exit_code)
            runtime_validator.assert_not_called()

    def test_keep_workdir_preserves_failed_owned_run(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            temp_parent = Path(parent)
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=parent,
            ), patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_core",
                return_value=[("sample", False, "failed")],
            ):
                exit_code = validate_skill_pack.main(
                    ["--suite", "core", "--keep-workdir"]
                )

            self.assertEqual(1, exit_code)
            retained = list(temp_parent.iterdir())
            self.assertEqual(1, len(retained))
            self.assertTrue(
                retained[0].name.startswith(
                    validate_skill_pack.VALIDATION_TRANSACTION_PREFIX
                )
            )
            self.assertTrue(
                (
                    retained[0]
                    / validate_skill_pack.VALIDATION_WORKDIR_NAME
                ).is_dir()
            )

    def test_keep_workdir_preserves_successful_owned_run(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            temp_parent = Path(parent)
            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=parent,
            ), patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_core",
                return_value=[("sample", True, "")],
            ):
                exit_code = validate_skill_pack.main(
                    ["--suite", "core", "--keep-workdir"]
                )

            self.assertEqual(0, exit_code)
            retained = list(temp_parent.iterdir())
            self.assertEqual(1, len(retained))
            self.assertTrue(
                (
                    retained[0]
                    / validate_skill_pack.VALIDATION_WORKDIR_NAME
                ).is_dir()
            )

    def test_successful_owned_run_is_removed_without_keep_workdir(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            temp_parent = Path(parent)

            def validate_with_artifact(
                _stata_binary: Path,
                work_root: Path,
                _selected_slugs: list[str] | None,
            ) -> list[tuple[str, bool, str]]:
                artifact = work_root / "core" / "sample" / "result.log"
                artifact.parent.mkdir(parents=True)
                artifact.write_text("validation output\n", encoding="utf-8")
                return [("sample", True, "")]

            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=parent,
            ), patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_core",
                side_effect=validate_with_artifact,
            ):
                exit_code = validate_skill_pack.main(["--suite", "core"])

            self.assertEqual(0, exit_code)
            self.assertEqual([], list(temp_parent.iterdir()))

    def test_failed_owned_run_is_removed_without_keep_workdir(self) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            temp_parent = Path(parent)

            def fail_with_artifact(
                _stata_binary: Path,
                work_root: Path,
                _selected_slugs: list[str] | None,
            ) -> list[tuple[str, bool, str]]:
                artifact = work_root / "core" / "sample" / "failure.log"
                artifact.parent.mkdir(parents=True)
                artifact.write_text("failed output\n", encoding="utf-8")
                return [("sample", False, "failed")]

            with patch.object(
                validate_skill_pack.tempfile,
                "gettempdir",
                return_value=parent,
            ), patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_core",
                side_effect=fail_with_artifact,
            ):
                exit_code = validate_skill_pack.main(["--suite", "core"])

            self.assertEqual(1, exit_code)
            self.assertEqual([], list(temp_parent.iterdir()))

    def test_nonowned_failed_fixture_is_retained_without_keep_workdir(
        self,
    ) -> None:
        with TemporaryDirectory(prefix="validation-parent-") as parent:
            work_root = Path(parent) / "failed"
            work_root.mkdir()
            with patch.object(validate_skill_pack, "lint_repo", return_value=[]), patch.object(
                validate_skill_pack, "detect_stata_binary", return_value=Path("/fake/stata")
            ), patch.object(
                validate_skill_pack,
                "validate_core",
                return_value=[("sample", False, "failed")],
            ), supplied_validation_workspace(work_root):
                exit_code = validate_skill_pack.main(["--suite", "core"])

            self.assertEqual(1, exit_code)
            self.assertTrue(work_root.is_dir())

    def test_keep_workdir_help_describes_opt_in_retention(self) -> None:
        help_text = " ".join(
            validate_skill_pack.build_parser().format_help().split()
        )

        self.assertIn(
            "Retain the run-private validation transaction",
            help_text,
        )
        self.assertIn(
            "ordinary completed runs remove their verified workspace",
            help_text,
        )


class DiagnosticSanitizationTests(unittest.TestCase):
    def test_sanitize_diagnostics_redacts_paths_and_license_metadata(self) -> None:
        work_root = Path("/private/tmp/stata-validation-secret")
        diagnostics = "\n".join(
            [
                f"failed under {work_root}/core",
                f"source: {validate_skill_pack.REPO_ROOT}/content",
                "Licensed to: Example User",
                "Serial number: 123456789",
                "ordinary diagnostic",
            ]
        )

        sanitized = validate_skill_pack.sanitize_diagnostics(
            diagnostics,
            work_root=work_root,
        )

        self.assertNotIn(str(work_root), sanitized)
        self.assertNotIn(str(validate_skill_pack.REPO_ROOT), sanitized)
        self.assertNotIn("Example User", sanitized)
        self.assertNotIn("123456789", sanitized)
        self.assertIn("<WORKDIR>", sanitized)
        self.assertIn("<REPO_ROOT>", sanitized)
        self.assertIn("[Stata license metadata redacted]", sanitized)
        self.assertIn("ordinary diagnostic", sanitized)


if __name__ == "__main__":
    unittest.main()
